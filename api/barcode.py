"""Barcode scan endpoint (F-8).

Receives a POST from the scanner Mini App with:

    ean         — 8-13 digit string
    initData    — Telegram WebApp signed payload (HMAC-verified server-side)

Flow:

    1. Verify ``initData`` to recover the trusted user_id.
    2. Apply the per-user OpenAI quota counter (this lookup is *cheap* — no
       OpenAI call — but a flood of barcode scans still costs us; reuse the
       existing meal_analysis quota so abuse is bounded).
    3. Look up the EAN in Open Food Facts.
    4a. Hit:   stash a "pending barcode" analysis with per-100g nutriments,
              send a Telegram message with portion-picker buttons.
    4b. Miss: send a friendly fallback message asking the user to type the
              product description in chat (which the existing manual flow
              picks up).

The browser only needs ``{ok: true}`` / ``{ok: false, error: "..."}`` — it
closes the WebApp once the bot responds in chat.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_THIS)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.config import LOCAL_TZ
from lib.database import (
    get_conn,
    init_db,
    get_profile,
    save_pending_analysis,
    consume_quota,
)
from lib.initdata import verify_init_data
from lib.log import setup_sentry, http_handler, error, info
from lib.off import lookup_product
from lib.telegram_helpers import send_message
from lib.formatters import (
    BARCODE_NOT_FOUND,
    BARCODE_LOOKUP_FAILED,
    BARCODE_FOUND_HEADER,
)


setup_sentry("barcode")


# Daily cap on barcode scans per user (re-uses the meal_analysis bucket).
_QUOTA_KIND = "meal_analysis"
_QUOTA_LIMIT_PER_DAY = 50  # mirrors LIMIT_PHOTO_PER_DAY default

_RESPONSE_OK    = {"ok": True}
_RESPONSE_404   = {"ok": False, "error": "Не знайшов цей штрих-код у базі."}
_RESPONSE_ERROR = {"ok": False, "error": "Internal error"}


def _meal_type_by_local_hour(profile: dict | None) -> str:
    """Pick the most likely meal_type for the current local hour."""
    from datetime import datetime
    try:
        from lib.datehelpers import now_user
        hr = now_user(profile).hour
    except Exception:
        hr = datetime.now(LOCAL_TZ).hour
    if 5 <= hr < 11:
        return "breakfast"
    if 11 <= hr < 16:
        return "lunch"
    if 16 <= hr < 21:
        return "dinner"
    return "snack"


def _portion_keyboard(serving_size_g: int | None) -> dict:
    """Inline keyboard for picking the portion grams of a scanned product.

    If the OFF product reports a serving size, surface it as an extra
    button at the top so the most-common case is one tap.
    """
    rows = []
    if serving_size_g and 5 <= serving_size_g <= 5000:
        rows.append([{
            "text": f"📦 Порція: {int(serving_size_g)}г",
            "callback_data": f"barcode:g:{int(serving_size_g)}",
        }])
    rows.append([
        {"text": "50г",  "callback_data": "barcode:g:50"},
        {"text": "100г", "callback_data": "barcode:g:100"},
        {"text": "150г", "callback_data": "barcode:g:150"},
        {"text": "200г", "callback_data": "barcode:g:200"},
    ])
    rows.append([{"text": "✏️ Інша кількість", "callback_data": "barcode:g:custom"}])
    rows.append([{"text": "❌ Скасувати",       "callback_data": "barcode:cancel"}])
    return {"inline_keyboard": rows}


class handler(BaseHTTPRequestHandler):
    @http_handler("barcode")
    def do_POST(self):
        # Read body (form-encoded multipart from fetch).
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""

        ctype = (self.headers.get("content-type") or "").lower()
        if ctype.startswith("multipart/form-data"):
            ean, init_data = _parse_multipart(body, ctype)
        elif ctype.startswith("application/x-www-form-urlencoded"):
            params = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
            ean = (params.get("ean") or [""])[0]
            init_data = (params.get("initData") or [""])[0]
        elif ctype.startswith("application/json"):
            try:
                payload = json.loads(body.decode("utf-8", errors="replace") or "{}")
            except (TypeError, ValueError):
                payload = {}
            ean = str(payload.get("ean") or "")
            init_data = str(payload.get("initData") or "")
        else:
            self._respond(400, {"ok": False, "error": "Unsupported content-type"})
            return

        ean = (ean or "").strip()
        if not ean.isdigit() or not (8 <= len(ean) <= 13):
            self._respond(400, {"ok": False, "error": "Bad EAN"})
            return

        user = verify_init_data(init_data)
        if not user:
            self._respond(401, {"ok": False, "error": "Auth failed"})
            return

        user_id = int(user["id"])
        chat_id = user_id  # WebApp init_data is per-user; no chat context

        conn = get_conn()
        try:
            init_db(conn)
            profile = get_profile(conn, user_id)
            if not profile:
                self._respond(403, {"ok": False, "error": "Onboard first via /start"})
                return

            # Quota check — barcode lookups are cheap but a flood is still
            # spam. Bucket alongside photo analysis at 50/day.
            try:
                used = consume_quota(conn, user_id, _QUOTA_KIND)
                if used > _QUOTA_LIMIT_PER_DAY:
                    self._respond(429, {"ok": False, "error": "Денний ліміт"})
                    return
            except Exception as qx:
                error("barcode_quota_failed", exc=qx, user_id=user_id)

            try:
                product = lookup_product(ean)
            except Exception as ox:
                error("off_lookup_failed", exc=ox, ean=ean)
                send_message(chat_id, BARCODE_LOOKUP_FAILED)
                self._respond(502, _RESPONSE_ERROR)
                return

            if product is None:
                info("barcode_not_found", ean=ean, user_id=user_id)
                send_message(chat_id, BARCODE_NOT_FOUND.format(ean=ean))
                self._respond(404, _RESPONSE_404)
                return

            # Stash the per-100g product so the portion picker callback can
            # rebuild a full meal entry. We piggyback on pending_analyses
            # so cleanup, expiry, and per-user single-row constraints come
            # for free. The "analysis" we save here is intentionally NOT a
            # logged meal — its calorie numbers are per-100g until the user
            # picks a portion.
            meal_type = _meal_type_by_local_hour(profile)
            pseudo_analysis = {
                "_pending_kind":  "barcode",
                "ean":            product["ean"],
                "name":           product["name"],
                "brand":          product["brand"],
                "per_100g":       product["per_100g"],
                "serving_size_g": product["serving_size_g"],
            }
            try:
                save_pending_analysis(
                    conn, user_id, meal_type, pseudo_analysis,
                    photo_file_id=None, text_description=None,
                    raw_response=json.dumps(product, ensure_ascii=False),
                )
            except Exception as sx:
                error("barcode_save_pending_failed", exc=sx, user_id=user_id)
                self._respond(500, _RESPONSE_ERROR)
                return

            # Send the portion-picker message.
            send_message(
                chat_id,
                BARCODE_FOUND_HEADER.format(
                    name=product["name"],
                    brand=product["brand"] or "—",
                    kcal=int(round(product["per_100g"]["calories"])),
                    p=int(round(product["per_100g"]["protein_g"])),
                    f=int(round(product["per_100g"]["fat_g"])),
                    c=int(round(product["per_100g"]["carbs_g"])),
                ),
                reply_markup=_portion_keyboard(product["serving_size_g"]),
            )
            self._respond(200, _RESPONSE_OK)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _respond(self, status: int, payload: dict) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _parse_multipart(body: bytes, content_type: str) -> tuple[str, str]:
    """Best-effort multipart/form-data extraction for ``ean`` + ``initData``.

    We only need two short text fields, so we don't pull in the full
    ``email.parser`` machinery — keeps cold-start cheap.
    """
    try:
        boundary_marker = "boundary="
        idx = content_type.find(boundary_marker)
        if idx < 0:
            return "", ""
        boundary = ("--" + content_type[idx + len(boundary_marker):].split(";")[0].strip())
        bb = boundary.encode("utf-8")
        parts = body.split(bb)
        out: dict[str, str] = {}
        for part in parts:
            part = part.lstrip(b"\r\n")
            if not part or part.startswith(b"--"):
                continue
            head, _, value = part.partition(b"\r\n\r\n")
            if not head:
                continue
            head_text = head.decode("utf-8", errors="replace")
            # Expect: Content-Disposition: form-data; name="ean"
            name = ""
            for line in head_text.split("\r\n"):
                if line.lower().startswith("content-disposition:"):
                    for piece in line.split(";"):
                        piece = piece.strip()
                        if piece.startswith("name="):
                            name = piece[5:].strip().strip('"')
                            break
                    break
            if not name:
                continue
            value = value.rstrip(b"\r\n--")
            out[name] = value.decode("utf-8", errors="replace").strip()
        return out.get("ean", ""), out.get("initData", "")
    except Exception:
        return "", ""
