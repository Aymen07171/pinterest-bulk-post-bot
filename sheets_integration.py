"""Google Sheets content source and publishing-state helpers."""

from __future__ import annotations

import datetime as dt
import logging
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import requests
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account


logger = logging.getLogger("PinterestBot")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]
SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets"
DRIVE_API = "https://www.googleapis.com/drive/v3/files"
SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}

FIELD_ALIASES = {
    "title": ["product title", "design title", "pin title", "listing title", "product name", "design name", "title", "name"],
    "image": ["mockup image url", "mockup image link", "mockup image", "mockup url", "mockup link", "mockup", "product image url", "product image", "image url", "image link", "image path", "image", "photo"],
    "keywords": ["seo keywords", "search keywords", "keyword tags", "keywords", "keyword", "tags"],
    "description": ["product description", "listing description", "pin description", "description", "details"],
    "url": ["product listing url", "product url", "listing url", "destination url", "product link", "listing link", "url", "link"],
    "board": ["pinterest board", "board name", "board", "pinterest category"],
    "category": ["product category", "design category", "category", "niche"],
    "status": ["publishing status", "publish status", "pin status", "status"],
    "selection": ["publish", "publish pin", "selected", "select", "queue", "ready to publish", "publish this"],
    "pin_id": ["pinterest pin id", "pin id"],
    "pin_url": ["pinterest pin url", "pin url"],
    "error": ["publish error", "last publish error", "last error"],
    "updated_at": ["last updated", "updated at", "publish updated at"],
    "published_at": ["published at", "published date", "publication date", "pinterest published at", "pin published at"],
    "published_title": ["published title", "final published title", "pinterest published title"],
    "published_description": ["published description", "final published description", "pinterest published description"],
    "csv_exported_at": ["pinterest csv exported at", "csv exported at"],
    "scheduled_at": ["scheduled at", "schedule at", "publish at", "publish date", "schedule date", "scheduled date"],
}

OUTPUT_HEADERS = {
    "selection": "Publish",
    "status": "Status",
    "pin_id": "Pinterest Pin ID",
    "pin_url": "Pinterest Pin URL",
    "error": "Publish Error",
    "updated_at": "Updated At",
    "published_at": "Published At",
    "published_title": "Final Published Title",
    "published_description": "Final Published Description",
    "csv_exported_at": "Pinterest CSV Exported At",
}

PINTEREST_MAX_TITLE = 100
PINTEREST_MAX_DESCRIPTION = 500

CONTEXT_ALIASES = {
    "category": ["product category", "design category", "category", "niche"],
    "product_type": ["product type", "item type", "product"],
    "style": ["style", "design style", "aesthetic"],
    "color": ["color", "colour", "main color", "primary color"],
    "material": ["material", "case material"],
    "occasion": ["occasion", "holiday", "event"],
    "recipient": ["recipient", "audience", "target audience", "for"],
    "brand": ["brand", "collection"],
    "features": ["features", "design features", "highlights"],
}


def normalize_header(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def spreadsheet_reference(value):
    """Return (spreadsheet ID, gid from URL) for an ID or Sheets URL."""
    text = str(value or "").strip()
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", text)
    spreadsheet_id = match.group(1) if match else text
    gid_match = re.search(r"(?:[?#&])gid=(\d+)", text)
    return spreadsheet_id, int(gid_match.group(1)) if gid_match else None


def column_letter(index):
    """Convert a zero-based column index to its A1 column name."""
    result = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _parse_google_error(response):
    try:
        payload = response.json()
        return payload.get("error", {}).get("message", response.reason)
    except (ValueError, AttributeError):
        return getattr(response, "reason", "request failed")


class GoogleSheetsClient:
    """Read product rows and write per-row Pinterest results."""

    def __init__(self, config, project_dir):
        self.config = config
        self.project_dir = Path(project_dir).resolve()
        raw_reference = config.get("spreadsheet_url") or config.get("spreadsheet_id")
        self.spreadsheet_id, url_gid = spreadsheet_reference(raw_reference)
        if not self.spreadsheet_id:
            raise ValueError("Set google_sheets.spreadsheet_id or spreadsheet_url in config.json.")

        credential_path = Path(config.get("service_account_file", "credentials/google-service-account.json"))
        if not credential_path.is_absolute():
            credential_path = self.project_dir / credential_path
        if not credential_path.is_file():
            raise FileNotFoundError(
                f"Google service-account key not found: {credential_path}. "
                "Create a service account, share the sheet with its email, and set this path."
            )

        try:
            credentials = service_account.Credentials.from_service_account_file(
                str(credential_path), scopes=SCOPES
            )
        except (ValueError, OSError) as exc:
            raise ValueError(f"Could not load Google service-account credentials: {exc}") from exc

        self.session = AuthorizedSession(credentials)
        self.drive_session = self.session
        self.headers = []
        self.field_indexes = {}
        self.image_indexes = []
        self.keyword_indexes = []
        self.output_indexes = {}

        metadata = self._request("GET", self._spreadsheet_url, params={
            "fields": "properties.title,sheets.properties(sheetId,title,index,gridProperties(columnCount,rowCount))"
        })
        available_sheets = [item.get("properties", {}) for item in metadata.get("sheets", [])]
        configured_name = config.get("worksheet_name")
        configured_gid = config.get("worksheet_gid", url_gid)
        selected_sheet = None
        if configured_name:
            selected_sheet = next((s for s in available_sheets if s.get("title") == configured_name), None)
        elif configured_gid is not None:
            selected_sheet = next((s for s in available_sheets if str(s.get("sheetId")) == str(configured_gid)), None)
        elif available_sheets:
            selected_sheet = available_sheets[0]
        if not selected_sheet:
            requested = configured_name if configured_name else configured_gid
            raise ValueError(f"Could not find worksheet {requested!r} in the configured spreadsheet.")

        self.worksheet_title = selected_sheet["title"]
        self.worksheet_id = selected_sheet["sheetId"]
        self.grid_column_count = selected_sheet.get("gridProperties", {}).get("columnCount", 26)
        self.header_row = max(1, int(config.get("header_row", 1)))
        escaped_title = self.worksheet_title.replace("'", "''")
        self.sheet_range = f"'{escaped_title}'"

    @property
    def _spreadsheet_url(self):
        return f"{SHEETS_API}/{quote(self.spreadsheet_id, safe='')}"

    def _request(self, method, url, **kwargs):
        last_response = None
        for attempt in range(5):
            try:
                response = self.session.request(method, url, timeout=30, **kwargs)
            except requests.RequestException as exc:
                if attempt == 4:
                    raise RuntimeError(f"Google API request failed: {exc}") from exc
                time.sleep(2 ** attempt)
                continue
            last_response = response
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            if not response.ok:
                message = _parse_google_error(response)
                raise RuntimeError(f"Google API returned HTTP {response.status_code}: {message}")
            if response.status_code == 204 or not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise RuntimeError("Google API returned an invalid JSON response.") from exc
        if last_response is not None:
            raise RuntimeError(
                f"Google API remained unavailable (HTTP {last_response.status_code}): "
                f"{_parse_google_error(last_response)}"
            )
        raise RuntimeError("Google API request failed without a response.")

    def _values_url(self, range_a1):
        return f"{self._spreadsheet_url}/values/{quote(range_a1, safe='')}"

    def _find_column(self, headers, field, configured_name=None):
        normalized = [normalize_header(item) for item in headers]
        if configured_name:
            target = normalize_header(configured_name)
            if target in normalized:
                return normalized.index(target)
        for alias in FIELD_ALIASES.get(field, []):
            target = normalize_header(alias)
            if target in normalized:
                return normalized.index(target)
        return None

    def load_products(self, *, initialize_status=True):
        range_a1 = f"{self.sheet_range}!A{self.header_row}:ZZ"
        result = self._request(
            "GET",
            self._values_url(range_a1),
            params={"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"},
        )
        values = result.get("values", [])
        if not values:
            raise ValueError("The selected worksheet is empty or does not contain the configured header row.")

        self.headers = [str(value).strip() for value in values[0]]
        if not any(self.headers):
            raise ValueError("The configured Google Sheets header row is blank.")

        output_config = self.config.get("output_columns", {})
        missing_outputs = []
        for field, default_name in OUTPUT_HEADERS.items():
            configured_name = output_config.get(field, default_name)
            index = self._find_column(self.headers, field, configured_name)
            if index is None:
                missing_outputs.append((field, configured_name))
            else:
                self.output_indexes[field] = index

        if missing_outputs:
            first_new_index = max((i for i, name in enumerate(self.headers) if name), default=-1) + 1
            additions = [name for _, name in missing_outputs]
            required_column_count = first_new_index + len(additions)
            if required_column_count > self.grid_column_count:
                self._request(
                    "POST",
                    f"{self._spreadsheet_url}:batchUpdate",
                    json={"requests": [{
                        "appendDimension": {
                            "sheetId": self.worksheet_id,
                            "dimension": "COLUMNS",
                            "length": required_column_count - self.grid_column_count,
                        }
                    }]},
                )
                self.grid_column_count = required_column_count
            start = column_letter(first_new_index)
            end = column_letter(first_new_index + len(additions) - 1)
            output_range = f"{self.sheet_range}!{start}{self.header_row}:{end}{self.header_row}"
            self._request(
                "PUT",
                self._values_url(output_range),
                params={"valueInputOption": "USER_ENTERED"},
                json={"majorDimension": "ROWS", "values": [additions]},
            )
            for offset, (field, _) in enumerate(missing_outputs):
                self.output_indexes[field] = first_new_index + offset
            self.headers.extend(additions)
            logger.info("Added publishing tracking columns to worksheet '%s'.", self.worksheet_title)

        # Resolve row data columns after adding tracking headers.
        self.field_indexes = {}
        column_config = self.config.get("column_map", {})
        for field in ("title", "image", "keywords", "description", "url", "board", "category", "scheduled_at"):
            configured_name = column_config.get(field)
            self.field_indexes[field] = self._find_column(self.headers, field, configured_name)

        configured_image_columns = self.config.get("image_columns") or column_config.get("image_columns")
        if configured_image_columns:
            if isinstance(configured_image_columns, str):
                configured_image_columns = [configured_image_columns]
            self.image_indexes = [
                self.headers.index(next(h for h in self.headers if normalize_header(h) == normalize_header(name)))
                for name in configured_image_columns
                if any(normalize_header(h) == normalize_header(name) for h in self.headers)
            ]
        else:
            self.image_indexes = [
                index for index, name in enumerate(self.headers)
                if re.fullmatch(r"mockup\d+url", normalize_header(name))
            ]
            if not self.image_indexes and self.field_indexes.get("image") is not None:
                self.image_indexes = [self.field_indexes["image"]]
        if self.image_indexes:
            self.field_indexes["image"] = self.image_indexes[0]

        configured_keyword_columns = self.config.get("keyword_columns") or column_config.get("keyword_columns")
        if configured_keyword_columns:
            if isinstance(configured_keyword_columns, str):
                configured_keyword_columns = [configured_keyword_columns]
            self.keyword_indexes = [
                index for index, header in enumerate(self.headers)
                if normalize_header(header) in {normalize_header(name) for name in configured_keyword_columns}
            ]
        else:
            keyword_index = self.field_indexes.get("keywords")
            if keyword_index is not None:
                self.keyword_indexes = [keyword_index]
            else:
                self.keyword_indexes = [
                    index for index, name in enumerate(self.headers)
                    if re.fullmatch(r"(?:tag|keyword|seokeyword)\d+", normalize_header(name))
                ]
        if self.keyword_indexes:
            self.field_indexes["keywords"] = self.keyword_indexes[0]

        missing_required = [field for field in ("title", "image") if self.field_indexes.get(field) is None]
        if missing_required:
            names = {"title": "product/design title", "image": "mockup image URL or path"}
            raise ValueError(
                "Could not find required Google Sheet column(s): "
                + ", ".join(names[field] for field in missing_required)
                + ". Add the column or map it in google_sheets.column_map."
            )

        # Values.get normally returns a rendered cell value. Fetch image formulas separately so
        # a cell containing =IMAGE("https://...") can also be used as a mockup source.
        first_image_index = min(self.image_indexes)
        last_image_index = max(self.image_indexes)
        image_range = (
            f"{self.sheet_range}!{column_letter(first_image_index)}{self.header_row}:"
            f"{column_letter(last_image_index)}"
        )
        formula_result = self._request(
            "GET",
            self._values_url(image_range),
            params={"majorDimension": "ROWS", "valueRenderOption": "FORMULA"},
        )
        image_formulas = formula_result.get("values", [])

        selection_name = self.config.get("selection_column", OUTPUT_HEADERS["selection"])
        self.selection_index = self._find_column(self.headers, "selection", selection_name)
        status_config = output_config.get("status", OUTPUT_HEADERS["status"])
        status_index = self._find_column(self.headers, "status", status_config)
        self.output_indexes["status"] = status_index

        products = []
        for offset, row in enumerate(values[1:], start=1):
            sheet_row = self.header_row + offset
            if not any(str(cell).strip() for cell in row):
                continue
            formula_row = offset < len(image_formulas) and bool(image_formulas[offset])
            if formula_row:
                for image_index in self.image_indexes:
                    formula_offset = image_index - first_image_index
                    formula_row_values = image_formulas[offset]
                    if formula_offset < len(formula_row_values):
                        image_formula = str(formula_row_values[formula_offset]).strip()
                        if image_formula.startswith("=") and image_index < len(row):
                            row[image_index] = image_formula
            raw = {name: str(row[i]).strip() if i < len(row) and row[i] is not None else ""
                   for i, name in enumerate(self.headers)}
            product = {"sheet_row": sheet_row, "raw": raw}
            for field, index in self.field_indexes.items():
                if field in {"image", "keywords"}:
                    continue
                product[field] = str(row[index]).strip() if index is not None and index < len(row) and row[index] is not None else ""
            product["image"] = next(
                (str(row[index]).strip() for index in self.image_indexes if index < len(row) and str(row[index]).strip()),
                "",
            )
            product["keywords"] = ", ".join(
                str(row[index]).strip()
                for index in self.keyword_indexes
                if index < len(row) and str(row[index]).strip()
            )
            product["status"] = self._cell(row, self.output_indexes.get("status"))
            product["selected"] = self._cell(row, self.selection_index)
            product["pin_id"] = self._cell(row, self.output_indexes.get("pin_id"))
            product["pin_url"] = self._cell(row, self.output_indexes.get("pin_url"))
            product["publish_error"] = self._cell(row, self.output_indexes.get("error"))
            product["csv_exported_at"] = self._cell(row, self.output_indexes.get("csv_exported_at"))
            product["scheduled_at"] = product.get("scheduled_at", "")
            product["context"] = self._context_values(row)
            products.append(product)

        # Give each row an explicit initial state and recognize manually-entered existing Pin links.
        initial_state_updates = []
        for product in products:
            if not product["status"]:
                product["status"] = "Published" if (product["pin_id"] or product["pin_url"]) else "Not Published"
                if initialize_status:
                    status_column = column_letter(self.output_indexes["status"])
                    initial_state_updates.append({
                        "range": f"{self.sheet_range}!{status_column}{product['sheet_row']}",
                        "values": [[product["status"]]],
                    })
        for offset in range(0, len(initial_state_updates), 500):
            self._request(
                "POST",
                f"{self._spreadsheet_url}/values:batchUpdate",
                json={
                    "valueInputOption": "RAW",
                    "data": initial_state_updates[offset:offset + 500],
                },
            )

        return products

    def _cell(self, row, index):
        return str(row[index]).strip() if index is not None and index < len(row) and row[index] is not None else ""

    def _context_values(self, row):
        result = {}
        configured_names = self.config.get("description_fields", [])
        fields = list(CONTEXT_ALIASES.items()) + [(f"custom_{i}", [name]) for i, name in enumerate(configured_names)]
        for key, aliases in fields:
            for alias in aliases:
                normalized_alias = normalize_header(alias)
                for index, header in enumerate(self.headers):
                    if normalize_header(header) == normalized_alias:
                        value = self._cell(row, index)
                        if value:
                            result[key] = value
                        break
                if key in result:
                    break
        return result

    def update_product(
        self, sheet_row, *, status=None, pin_id=None, pin_url=None, error=None,
        selected=None, published_at=None, published_title=None, published_description=None,
        csv_exported_at=None,
    ):
        values = {
            "selection": "TRUE" if selected is True else "FALSE" if selected is False else None,
            "status": status,
            "pin_id": pin_id,
            "pin_url": pin_url,
            "error": error,
            "updated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "published_at": published_at,
            "published_title": published_title,
            "published_description": published_description,
            "csv_exported_at": csv_exported_at,
        }
        updates = []
        for field, value in values.items():
            if value is None:
                continue
            index = self.output_indexes[field]
            column = column_letter(index)
            updates.append({
                "range": f"{self.sheet_range}!{column}{sheet_row}",
                "values": [[str(value)]],
            })
        if not updates:
            return
        self._request(
            "POST",
            f"{self._spreadsheet_url}/values:batchUpdate",
            json={"valueInputOption": "USER_ENTERED", "data": updates},
        )


def parse_schedule(value):
    """Parse common Google Sheets date/time formats into local-aware time."""
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError:
        parsed = None
        for fmt in (
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
            "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y %I:%M %p", "%m/%d/%Y",
            "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
        ):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone()


def _is_checked(value):
    return str(value or "").strip().casefold() in {"true", "yes", "y", "1", "checked", "publish"}


def rows_to_publish(products, explicit_rows=None, retry_failed=False, now=None):
    """Return rows selected by status, the sheet checkbox, or explicit row numbers."""
    explicit = set(explicit_rows or [])
    now = now or dt.datetime.now().astimezone()
    selected = []
    for product in products:
        row_number = product["sheet_row"]
        status = str(product.get("status", "")).strip().casefold()
        has_pin_reference = bool(product.get("pin_id") or product.get("pin_url"))
        if status == "published" or has_pin_reference:
            logger.info("Skipping sheet row %d because it is marked Published or has a Pin reference.", row_number)
            continue
        if status in {"processing", "publishing"}:
            logger.warning(
                "Skipping sheet row %d: it was left in %s state; review Pinterest before retrying.",
                row_number, product.get("status", "Processing"),
            )
            continue
        if row_number in explicit:
            selected.append(product)
            continue
        if status == "scheduled":
            schedule = parse_schedule(product.get("scheduled_at"))
            if schedule and schedule <= now:
                selected.append(product)
            continue
        schedule = parse_schedule(product.get("scheduled_at"))
        if schedule and schedule > now:
            continue
        if status == "ready" or _is_checked(product.get("selected")):
            selected.append(product)
        elif status in {"failed", "retry required"} and retry_failed:
            selected.append(product)
    return selected


def _split_keywords(value):
    chunks = re.split(r"[,;|\n\r]+", str(value or ""))
    terms = []
    seen = set()
    for chunk in chunks:
        item = re.sub(r"\s+", " ", chunk).strip(" \t-•")
        key = item.casefold()
        if item and key not in seen:
            terms.append(item)
            seen.add(key)
    return terms


def _clean_words(value):
    return re.findall(r"[\w']+|[^\w\s]", str(value or ""), flags=re.UNICODE)


def _join_title_tokens(tokens):
    text = " ".join(tokens)
    return re.sub(r"\s+([,.;:!?])", r"\1", text).strip(" ,.;:-")


def optimize_title(product, max_chars=PINTEREST_MAX_TITLE):
    """Create a readable title within Pinterest's 100-character title limit."""
    title = re.sub(r"\s+", " ", str(product.get("title", "")).strip())
    context = product.get("context", {})
    if not title:
        product_type = context.get("product_type") or context.get("category") or "Phone Case"
        keywords = _split_keywords(product.get("keywords"))
        lead = keywords[0].strip() if keywords else "Custom Design"
        title = f"{lead} {product_type}".strip()
    if len(title) <= max_chars:
        return title

    # Drop parenthetical/secondary copy before editing the main product phrase.
    title = re.sub(r"\s*\([^)]*\)", "", title).strip()
    # Remove common listing filler and repeated exact words, preserving their original order.
    filler = {
        "the", "and", "with", "for", "of", "a", "an", "to", "in", "on", "by",
        "perfect", "amazing", "beautiful", "gorgeous", "stunning", "musthave",
        "best", "gift", "gifts", "present", "unique", "trendy", "stylish",
        "premium", "quality", "official", "new", "sale", "limited", "exclusive",
    }
    repeated_product_phrases = (
        ("cell phone case", "phone case"),
        ("mobile phone case", "phone case"),
        ("phone cover", "phone case"),
        ("cell phone cover", "phone case"),
    )
    lowered = title.casefold()
    for redundant, canonical in repeated_product_phrases:
        if redundant in lowered and canonical in lowered:
            title = re.sub(re.escape(redundant), "", title, flags=re.IGNORECASE)
            lowered = title.casefold()

    tokens = _clean_words(title)
    seen = set()
    deduped = []
    for token in tokens:
        key = token.casefold()
        if re.match(r"\w", token) and key in seen:
            continue
        if re.match(r"\w", token):
            seen.add(key)
        deduped.append(token)
    tokens = deduped

    context_tokens = set()
    for value in (context.get("product_type", ""), context.get("category", "")):
        context_tokens.update(word.casefold() for word in _clean_words(value) if re.match(r"\w", word))
    keyword_terms = _split_keywords(product.get("keywords"))
    keyword_tokens = set()
    for phrase in keyword_terms[:5]:
        keyword_tokens.update(word.casefold() for word in _clean_words(phrase) if re.match(r"\w", word))
    low_value = filler | {"case", "cover", "phone", "iphone", "smartphone"}

    while len(_join_title_tokens(tokens)) > max_chars:
        word_positions = [i for i, token in enumerate(tokens) if re.match(r"\w", token)]
        if len(word_positions) <= 2:
            break
        candidates = []
        for position in word_positions:
            word = tokens[position].casefold()
            if word in context_tokens or word in keyword_tokens:
                continue
            # Remove low-information listing words first, then modifiers near the end.
            score = (0 if word in filler else 1 if word in low_value else 2)
            score += 1 if position < 3 else 0
            candidates.append((score, -position, position))
        if not candidates:
            # If all remaining terms are protected, discard the least specific word nearest
            # the end while keeping the primary phrase at the front.
            candidates = [(3, -position, position) for position in word_positions[2:]]
        remove_at = min(candidates)[2]
        tokens.pop(remove_at)
        while remove_at < len(tokens) and tokens[remove_at] in {",", ";", ":", "-", "|"}:
            tokens.pop(remove_at)

    optimized = _join_title_tokens(tokens)
    if len(optimized) <= max_chars and optimized:
        return optimized

    # A long/invalid source title should not be sent as-is. Build a compact semantic fallback.
    category = str(context.get("product_type") or context.get("category") or "Phone Case").split(">")[-1].strip()
    first_keyword = keyword_terms[0] if keyword_terms else "Custom Design"
    candidates = [f"{first_keyword} {category}".strip(), f"{first_keyword} Phone Case"]
    for candidate in candidates:
        candidate = re.sub(r"\s+", " ", candidate).strip()
        if candidate and len(candidate) <= max_chars:
            return candidate
    raise ValueError(f"Could not create a Pinterest title within {max_chars} characters from this row.")


def generate_description(product):
    """Create a factual, keyword-aware description no longer than 500 characters."""
    title = optimize_title(product)
    supplied = re.sub(r"\s+", " ", str(product.get("description", "")).strip())
    base = supplied or f"Discover {title}."
    context = product.get("context", {})

    extra_values = []
    for key, value in context.items():
        if key.startswith("custom_") or key in {"category", "product_type", "style", "color", "material", "occasion", "recipient", "brand", "features"}:
            value = re.sub(r"\s+", " ", str(value)).strip(" ,.;")
            if value and value.casefold() not in base.casefold() and value.casefold() != title.casefold():
                extra_values.append(value)

    existing = base.casefold()
    keywords = [term for term in _split_keywords(product.get("keywords")) if term.casefold() not in existing]
    # Avoid repeating near-identical long-tail terms in one description.
    natural_terms = []
    for term in keywords:
        if any(term.casefold() in used.casefold() or used.casefold() in term.casefold() for used in natural_terms):
            continue
        natural_terms.append(term)
        if len(natural_terms) == 3:
            break
    def render_keyword_sentence(terms):
        if not terms:
            return ""
        if len(terms) == 1:
            phrase = terms[0]
        elif len(terms) == 2:
            phrase = f"{terms[0]} and {terms[1]}"
        else:
            phrase = f"{terms[0]}, {terms[1]}, and {terms[2]}"
        return f"Explore this design for {phrase}."

    keyword_sentence = render_keyword_sentence(natural_terms)
    while natural_terms and len(keyword_sentence) > 150:
        natural_terms.pop()
        keyword_sentence = render_keyword_sentence(natural_terms)

    # Add the important search terms before optional metadata so keywords are not
    # silently dropped when the source description already uses most of the limit.
    detail_values = []
    for value in extra_values:
        candidate = "Design details include " + ", ".join(detail_values + [value]) + "."
        if len(candidate) <= 140:
            detail_values.append(value)
        if len(detail_values) == 2:
            break
    optional_sentence = "Design details include " + ", ".join(detail_values) + "." if detail_values else ""
    suffix_parts = [part for part in (keyword_sentence, optional_sentence) if part]
    suffix = " ".join(suffix_parts)
    available_for_base = PINTEREST_MAX_DESCRIPTION - len(suffix) - (1 if suffix else 0)

    def fit_at_sentence_boundary(value, limit):
        value = re.sub(r"\s+", " ", value).strip()
        if len(value) <= limit:
            return value
        kept = ""
        for sentence in re.split(r"(?<=[.!?])\s+", value):
            candidate = f"{kept} {sentence}".strip()
            if len(candidate) > limit:
                break
            kept = candidate
        if kept:
            return kept
        shortened = value[:max(1, limit - 1)].rsplit(" ", 1)[0].rstrip(" ,;:-")
        return f"{shortened}…" if shortened else ""

    if available_for_base < 30 and optional_sentence:
        suffix_parts = [keyword_sentence] if keyword_sentence else []
        suffix = " ".join(suffix_parts)
        available_for_base = PINTEREST_MAX_DESCRIPTION - len(suffix) - (1 if suffix else 0)
    base = fit_at_sentence_boundary(base, max(1, available_for_base))
    description = f"{base} {suffix}".strip() if suffix else base
    if len(description) > PINTEREST_MAX_DESCRIPTION:
        description = fit_at_sentence_boundary(description, PINTEREST_MAX_DESCRIPTION)
    return description


def validate_pin_data(product, image_path=None, *, title_limit=PINTEREST_MAX_TITLE,
                      description_limit=PINTEREST_MAX_DESCRIPTION):
    """Return concrete pre-publication validation errors for one row."""
    errors = []
    title = str(product.get("pin_title", "")).strip()
    description = str(product.get("pin_description", "")).strip()
    destination = str(product.get("destination_url", product.get("url", ""))).strip()
    board = str(product.get("pin_board", "")).strip()
    if not title:
        errors.append("Pin title is empty.")
    elif len(title) > title_limit:
        errors.append(f"Pin title is {len(title)} characters; the limit is {title_limit}.")
    if not description:
        errors.append("Pin description is empty.")
    elif len(description) > description_limit:
        errors.append(f"Pin description is {len(description)} characters; the limit is {description_limit}.")
    if not destination:
        errors.append("Destination URL is empty.")
    else:
        parsed = urlparse(destination)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append("Destination URL must be a valid HTTP or HTTPS URL.")
    if not board:
        errors.append("Pinterest board is not selected.")
    if image_path is not None:
        image = Path(image_path)
        if not image.is_file():
            errors.append("Mockup image file is missing.")
        elif image.stat().st_size <= 0:
            errors.append("Mockup image file is empty.")
        elif image.suffix.casefold() not in SUPPORTED_EXTENSIONS:
            errors.append(f"Mockup image type is unsupported: {image.suffix or 'unknown'}.")
        else:
            try:
                with image.open("rb") as source:
                    header = source.read(16)
            except OSError as exc:
                errors.append(f"Mockup image cannot be read: {exc}")
            else:
                ext = image.suffix.casefold()
                valid = (
                    (ext in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff"))
                    or (ext == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
                    or (ext == ".gif" and header.startswith((b"GIF87a", b"GIF89a")))
                    or (ext == ".webp" and header.startswith(b"RIFF") and header[8:12] == b"WEBP")
                    or (ext == ".bmp" and header.startswith(b"BM"))
                    or (ext in {".tif", ".tiff"} and header.startswith((b"II*\x00", b"MM\x00*")))
                )
                if not valid:
                    errors.append(f"Mockup file content does not match a supported {ext} image.")
    return errors


def resolve_board(product, category_boards=None, fallback=""):
    """Choose a row board, configured category board, or global fallback."""
    row_board = str(product.get("board", "")).strip()
    if row_board:
        return row_board
    category_boards = category_boards or {}
    context = product.get("context", {})
    category = product.get("category") or context.get("category") or context.get("product_type") or ""
    normalized_category = normalize_header(category)
    for key, board in category_boards.items():
        if normalize_header(key) == normalized_category and str(board).strip():
            return str(board).strip()
    return str(fallback or "").strip()


def _drive_file_id(image_reference):
    parsed = urlparse(image_reference)
    if parsed.netloc.casefold() not in {"drive.google.com", "docs.google.com"}:
        return None
    match = re.search(r"/file/d/([a-zA-Z0-9_-]+)", parsed.path)
    if match:
        return match.group(1)
    query = parse_qs(parsed.query)
    if query.get("id"):
        return query["id"][0]
    return None


def _image_url_from_formula(value):
    match = re.match(r"\s*=\s*(?:IMAGE|HYPERLINK)\(\s*\"([^\"]+)\"", str(value or ""), re.IGNORECASE)
    return match.group(1) if match else str(value or "").strip()


def _write_download(response, destination, reference):
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
    if content_type and not content_type.startswith("image/") and content_type not in {
        "application/octet-stream", "application/binary"
    }:
        raise ValueError(f"The mockup URL returned {content_type}, not an image.")
    ext = Path(urlparse(reference).path).suffix.casefold()
    if ext not in SUPPORTED_EXTENSIONS:
        ext = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/bmp": ".bmp",
            "image/tiff": ".tiff",
        }.get(content_type, "")
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"The mockup URL did not return a supported image (content type: {content_type or 'unknown'}).")
    output = destination.with_suffix(ext)
    total = 0
    with output.open("wb") as target:
        for chunk in response.iter_content(chunk_size=1024 * 128):
            if chunk:
                target.write(chunk)
                total += len(chunk)
                if total > 100 * 1024 * 1024:
                    target.close()
                    output.unlink(missing_ok=True)
                    raise ValueError("Mockup image exceeds the 100 MB download limit.")
    if total == 0:
        output.unlink(missing_ok=True)
        raise ValueError("The mockup image download was empty.")
    return output


def get_image_for_product(product, project_dir, temp_dir, session):
    """Resolve a local mockup path or download a URL/Drive file to a temp directory."""
    reference = _image_url_from_formula(product.get("image", ""))
    if not reference:
        raise ValueError("The row has no mockup image URL or path.")

    local_path = Path(reference).expanduser()
    candidates = [local_path] if local_path.is_absolute() else [Path(project_dir) / local_path, Path(project_dir) / "bulk_post_pinterest" / local_path]
    for candidate in candidates:
        if candidate.is_file() and candidate.suffix.casefold() in SUPPORTED_EXTENSIONS:
            return candidate.resolve()

    parsed = urlparse(reference)
    if parsed.scheme not in {"http", "https"}:
        raise FileNotFoundError(f"Mockup image path was not found: {reference}")

    destination = Path(temp_dir) / f"sheet-row-{product['sheet_row']}"
    drive_id = _drive_file_id(reference)
    try:
        if drive_id:
            response = session.get(
                f"{DRIVE_API}/{quote(drive_id, safe='')}",
                params={"alt": "media"},
                stream=True,
                timeout=60,
            )
        else:
            response = requests.get(reference, stream=True, timeout=60)
        if not response.ok:
            detail = _parse_google_error(response) if drive_id else response.reason
            raise ValueError(f"Mockup download returned HTTP {response.status_code}: {detail}")
        return _write_download(response, destination, reference)
    except requests.RequestException as exc:
        raise ValueError(f"Could not download the mockup image: {exc}") from exc
    finally:
        if "response" in locals():
            response.close()


def get_pinterest_bulk_media_url(product, drive_session):
    """Return an anonymous, direct image URL accepted by Pinterest's CSV importer."""
    reference = _image_url_from_formula(product.get("image", ""))
    if not reference:
        raise ValueError("The row has no mockup image URL.")

    drive_id = _drive_file_id(reference)
    if drive_id:
        try:
            metadata_response = drive_session.get(
                f"{DRIVE_API}/{quote(drive_id, safe='')}",
                params={"fields": "webContentLink"},
                timeout=30,
            )
        except requests.RequestException as exc:
            raise ValueError(f"Could not get the Google Drive image download link: {exc}") from exc
        if not metadata_response.ok:
            detail = _parse_google_error(metadata_response)
            raise ValueError(f"Google Drive could not provide a direct image link: {detail}")
        direct_url = metadata_response.json().get("webContentLink", "").strip()
        if not direct_url:
            raise ValueError("Google Drive did not provide a direct download link for this mockup.")
    else:
        direct_url = reference

    parsed = urlparse(direct_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Pinterest's CSV importer needs an HTTP or HTTPS image URL, not a local file path.")

    try:
        response = requests.get(
            direct_url,
            headers={"Range": "bytes=0-31"},
            stream=True,
            timeout=30,
        )
        if not response.ok:
            raise ValueError(f"Public image URL returned HTTP {response.status_code}.")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold()
        if not content_type.startswith("image/"):
            raise ValueError(
                "Pinterest's CSV importer needs a publicly fetchable image file; "
                f"the link returned {content_type or 'an unknown content type'}."
            )
        if not next(response.iter_content(chunk_size=32), b""):
            raise ValueError("Public image URL returned an empty file.")
    except requests.RequestException as exc:
        raise ValueError(f"Pinterest could not access the public mockup URL: {exc}") from exc
    finally:
        if "response" in locals():
            response.close()
    return direct_url


def parse_row_numbers(value):
    """Parse a comma-separated list of physical sheet row numbers."""
    if not value:
        return None
    try:
        numbers = {int(part.strip()) for part in str(value).split(",") if part.strip()}
    except ValueError as exc:
        raise ValueError("--rows must be comma-separated sheet row numbers, such as 2,5,8.") from exc
    if any(number < 2 for number in numbers):
        raise ValueError("--rows must point to data rows below the header row.")
    return numbers


def make_temp_image_dir():
    """Return a context manager whose downloaded images are removed after posting."""
    return tempfile.TemporaryDirectory(prefix="pinterest-sheet-images-")
