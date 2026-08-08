import azure.functions as func
import fitz
import concurrent.futures
import json
import logging

MAX_FILE_SIZE_MB = 25
MAX_FILE_SIZE_BYTES = int(MAX_FILE_SIZE_MB * 1024 * 1024)
# multipart/form-data has no base64 inflation to account for, but the request still
# carries the other form fields plus per-part headers/boundaries -- this margin covers
# that framing overhead for the cheap pre-parse Content-Length check below.
CONTENT_LENGTH_OVERHEAD_BYTES = 16 * 1024

ALLOWED_ACTIONS = {"process", "remove_password"}
ALLOWED_POSITIONS = {"top-left", "top-right", "bottom-left", "bottom-right", "center"}
MIN_FONTSIZE, MAX_FONTSIZE = 4, 72
MIN_PADDING, MAX_PADDING = 0, 200
MAX_PASSWORD_LEN = 128
MAX_WATERMARK_LEN = 500
MAX_PAGES = 2000
PROCESSING_TIMEOUT_SECONDS = 20
TRUTHY_STRINGS = {"true", "1", "yes"}
ALLOW_PERMISSION_FIELDS = (
    "allow_printing", "allow_high_res_printing", "allow_copying", "allow_editing",
    "allow_annotations", "allow_form_filling", "allow_accessibility", "allow_assembly",
)

# Verified empirically against the installed PyMuPDF 1.28.0 (MuPDF 1.29.0): a document
# with no permission restrictions at all reports doc.permissions == -4 (all permission
# bits set except the two reserved always-zero bits per the PDF spec's /P value), NOT -1.
FULL_PERMISSIONS = -4


def validate_hex_color(color_hex: str) -> bool:
    return len(color_hex) == 6 and all(c in "0123456789abcdefABCDEF" for c in color_hex)


def safe_int(value, default, field_name):
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"Invalid {field_name}: must be an integer")


def parse_bool_field(form, field_name, default):
    # Form data has no native boolean type -- only recognized truthy strings count.
    # A field that's absent entirely gets `default`; a field that's present but not
    # a recognized truthy string resolves to False, never guessed as True.
    if field_name not in form:
        return default
    return form.get(field_name, "").strip().lower() in TRUTHY_STRINGS


def main(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("PDF processor triggered.")
    doc = None

    try:
        raw_content_length = req.headers.get("Content-Length")
        if raw_content_length is not None:
            try:
                content_length = int(raw_content_length)
            except ValueError:
                content_length = None
            if content_length is not None and content_length > MAX_FILE_SIZE_BYTES + CONTENT_LENGTH_OVERHEAD_BYTES:
                return func.HttpResponse(
                    json.dumps({"error": f"File exceeds maximum size of {MAX_FILE_SIZE_MB} MB"}),
                    mimetype="application/json",
                    status_code=413
                )

        # req.files / req.form are backed by werkzeug's multipart parser (a hard
        # dependency of azure-functions, already installed -- no requirements.txt
        # change needed). A non-multipart or malformed body simply parses to empty
        # MultiDicts rather than raising, so a missing/invalid pdf part below is
        # reported the same way regardless of why it's missing.
        pdf_file = req.files.get("pdf")
        if pdf_file is None:
            return func.HttpResponse(
                json.dumps({"error": "Missing pdf field"}),
                mimetype="application/json",
                status_code=400
            )

        pdf_bytes = pdf_file.read()

        if not pdf_bytes:
            return func.HttpResponse(
                json.dumps({"error": "Missing pdf field"}),
                mimetype="application/json",
                status_code=400
            )

        if len(pdf_bytes) > MAX_FILE_SIZE_BYTES:
            return func.HttpResponse(
                json.dumps({"error": f"File exceeds maximum size of {MAX_FILE_SIZE_MB} MB"}),
                mimetype="application/json",
                status_code=413
            )

        action = req.form.get("action", "process")
        if action not in ALLOWED_ACTIONS:
            return func.HttpResponse(
                json.dumps({"error": f"Invalid action. Use one of {sorted(ALLOWED_ACTIONS)}."}),
                mimetype="application/json",
                status_code=400
            )

        watermark_text = req.form.get("watermark", "")
        password = req.form.get("password", "")
        # Track presence separately from value: a document whose real password is the
        # empty string (e.g. owner-password-only PDFs with an empty user password) must
        # be distinguishable from a request that omitted current_password altogether.
        # Verified empirically that req.form (a werkzeug ImmutableMultiDict) supports
        # the same "in" semantics as a JSON dict here: a field sent with an empty value
        # is present ("current_password" in req.form -> True, .get -> ''), while a field
        # never sent at all is absent (-> False, .get -> None).
        current_password_provided = "current_password" in req.form
        current_password = req.form.get("current_password") or ""
        position = req.form.get("position", "top-right")
        first_page_only = parse_bool_field(req.form, "first_page_only", False)
        color_hex = req.form.get("color", "4C5270").lstrip("#")

        try:
            fontsize = safe_int(req.form.get("fontsize", 7), 7, "fontsize")
            padding_top = safe_int(req.form.get("padding_top", 5), 5, "padding_top")
            padding_right = safe_int(req.form.get("padding_right", 0), 0, "padding_right")
        except ValueError as e:
            return func.HttpResponse(
                json.dumps({"error": str(e)}),
                mimetype="application/json",
                status_code=400
            )

        if not (MIN_FONTSIZE <= fontsize <= MAX_FONTSIZE):
            return func.HttpResponse(
                json.dumps({"error": f"Invalid fontsize: must be between {MIN_FONTSIZE} and {MAX_FONTSIZE}"}),
                mimetype="application/json",
                status_code=400
            )

        if not (MIN_PADDING <= padding_top <= MAX_PADDING) or not (MIN_PADDING <= padding_right <= MAX_PADDING):
            return func.HttpResponse(
                json.dumps({"error": f"Invalid padding: must be between {MIN_PADDING} and {MAX_PADDING}"}),
                mimetype="application/json",
                status_code=400
            )

        if position not in ALLOWED_POSITIONS:
            return func.HttpResponse(
                json.dumps({"error": f"Invalid position. Use one of {sorted(ALLOWED_POSITIONS)}."}),
                mimetype="application/json",
                status_code=400
            )

        if watermark_text and len(watermark_text) > MAX_WATERMARK_LEN:
            return func.HttpResponse(
                json.dumps({"error": f"Watermark text too long: max {MAX_WATERMARK_LEN} characters"}),
                mimetype="application/json",
                status_code=400
            )

        if password and len(password) > MAX_PASSWORD_LEN:
            return func.HttpResponse(
                json.dumps({"error": f"Password too long: max {MAX_PASSWORD_LEN} characters"}),
                mimetype="application/json",
                status_code=400
            )

        owner_password = req.form.get("owner_password", "")

        if owner_password and len(owner_password) > MAX_PASSWORD_LEN:
            return func.HttpResponse(
                json.dumps({"error": f"Owner password too long: max {MAX_PASSWORD_LEN} characters"}),
                mimetype="application/json",
                status_code=400
            )

        if watermark_text and not validate_hex_color(color_hex):
            return func.HttpResponse(
                json.dumps({"error": "Invalid color format. Use HEX format like '4C5270'."}),
                mimetype="application/json",
                status_code=400
            )

        # A field that's absent entirely is not a restriction request. Only a field
        # explicitly present with a value resolving to False counts -- this presence
        # check is tracked separately from parse_bool_field's default-substitution,
        # the same "presence, not just value" pattern used for current_password_provided.
        if not owner_password:
            restricted_without_owner_password = [
                field for field in ALLOW_PERMISSION_FIELDS
                if field in req.form and not parse_bool_field(req.form, field, True)
            ]
            if restricted_without_owner_password:
                fields_str = ", ".join(f"{field}=false" for field in restricted_without_owner_password)
                return func.HttpResponse(
                    json.dumps({"error": f"{fields_str} was requested, but owner_password was not provided. "
                                          "Restrictions require owner_password to be set."}),
                    mimetype="application/json",
                    status_code=400
                )

        # Per rule 1: when owner_password isn't provided, the allow_* fields are not
        # parsed or validated at all, so they can't silently influence behavior --
        # output_permissions stays None and the encryption call below is untouched.
        output_permissions = None
        if owner_password:
            allow_printing = parse_bool_field(req.form, "allow_printing", True)
            allow_high_res_printing = parse_bool_field(req.form, "allow_high_res_printing", True)
            allow_copying = parse_bool_field(req.form, "allow_copying", True)
            allow_editing = parse_bool_field(req.form, "allow_editing", True)
            allow_annotations = parse_bool_field(req.form, "allow_annotations", True)
            allow_form_filling = parse_bool_field(req.form, "allow_form_filling", True)
            allow_accessibility = parse_bool_field(req.form, "allow_accessibility", True)
            allow_assembly = parse_bool_field(req.form, "allow_assembly", True)

            if all([allow_printing, allow_high_res_printing, allow_copying, allow_editing,
                    allow_annotations, allow_form_filling, allow_accessibility, allow_assembly]):
                return func.HttpResponse(
                    json.dumps({"error": "owner_password was provided but no permissions were restricted "
                                          "-- specify at least one allow_* field as false, or omit "
                                          "owner_password entirely."}),
                    mimetype="application/json",
                    status_code=400
                )

            output_permissions = 0
            if allow_printing:
                output_permissions |= fitz.PDF_PERM_PRINT
            if allow_high_res_printing:
                output_permissions |= fitz.PDF_PERM_PRINT_HQ
            if allow_copying:
                output_permissions |= fitz.PDF_PERM_COPY
            if allow_editing:
                output_permissions |= fitz.PDF_PERM_MODIFY
            if allow_annotations:
                output_permissions |= fitz.PDF_PERM_ANNOTATE
            if allow_form_filling:
                output_permissions |= fitz.PDF_PERM_FORM
            if allow_accessibility:
                output_permissions |= fitz.PDF_PERM_ACCESSIBILITY
            if allow_assembly:
                output_permissions |= fitz.PDF_PERM_ASSEMBLE

        doc = fitz.open(stream=pdf_bytes, filetype="pdf")

        if doc.page_count > MAX_PAGES:
            doc.close()
            doc = None
            return func.HttpResponse(
                json.dumps({"error": f"Document exceeds maximum page count of {MAX_PAGES}"}),
                mimetype="application/json",
                status_code=413
            )

        if doc.page_count == 0:
            doc.close()
            doc = None
            return func.HttpResponse(
                json.dumps({"error": "Document has no pages"}),
                mimetype="application/json",
                status_code=400
            )

        # doc.is_encrypted / doc.needs_pass alone miss owner-password-only documents:
        # PyMuPDF auto-authenticates those with an empty user password at open time, so
        # needs_pass reads False even though the file IS protected. doc.permissions still
        # reflects the restricted /P value in that case, so check both.
        is_protected = bool(doc.needs_pass) or doc.permissions != FULL_PERMISSIONS
        auth_level = 0

        if is_protected:
            if not current_password_provided:
                return func.HttpResponse(
                    json.dumps({"error": "Document is protected. Provide current_password to proceed."}),
                    mimetype="application/json",
                    status_code=400
                )
            # authenticate() returns 0 on failure; on success it returns a bitmask where
            # bit 2 (value 4) indicates owner-level access and bit 1 (value 2) indicates
            # user-level access (both bits can be set at once) -- verified empirically
            # against the installed PyMuPDF 1.28.0, since this isn't exposed as a named
            # constant and its exact semantics are version-dependent.
            auth_level = doc.authenticate(current_password)
            if not auth_level:
                return func.HttpResponse(
                    json.dumps({"error": "Incorrect current_password"}),
                    mimetype="application/json",
                    status_code=401
                )

        if action == "remove_password" and is_protected and not (auth_level & 4):
            return func.HttpResponse(
                json.dumps({"error": "Owner-level credentials are required to remove protection."}),
                mimetype="application/json",
                status_code=403
            )

        if action == "process" and is_protected and not password:
            return func.HttpResponse(
                json.dumps({"error": "Document is protected. Provide a new password for the output document."}),
                mimetype="application/json",
                status_code=400
            )

        def _run_processing() -> bytes:
            if action == "remove_password":
                result = doc.tobytes(encryption=fitz.PDF_ENCRYPT_NONE)
            else:
                if watermark_text:
                    r = int(color_hex[0:2], 16) / 255
                    g = int(color_hex[2:4], 16) / 255
                    b = int(color_hex[4:6], 16) / 255

                    pages = [doc[0]] if first_page_only else doc

                    for page in pages:
                        rect = page.rect
                        text_width = fitz.get_text_length(watermark_text, fontsize=fontsize)

                        if position == "top-left":
                            x, y = padding_right, padding_top + fontsize
                        elif position == "top-right":
                            x, y = rect.width - text_width - padding_right, padding_top + fontsize
                        elif position == "bottom-left":
                            x, y = padding_right, rect.height - padding_top
                        elif position == "bottom-right":
                            x, y = rect.width - text_width - padding_right, rect.height - padding_top
                        else:
                            x, y = (rect.width - text_width) / 2, rect.height / 2

                        page.insert_text(
                            (x, y),
                            watermark_text,
                            fontsize=fontsize,
                            color=(r, g, b)
                        )

                if owner_password:
                    result = doc.tobytes(
                        encryption=fitz.PDF_ENCRYPT_AES_256,
                        user_pw=password if password else None,
                        owner_pw=owner_password,
                        permissions=output_permissions
                    )
                else:
                    result = doc.tobytes(
                        encryption=fitz.PDF_ENCRYPT_AES_256 if password else fitz.PDF_ENCRYPT_NONE,
                        user_pw=password if password else None,
                        owner_pw=password if password else None
                    )

            return result

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            output = executor.submit(_run_processing).result(timeout=PROCESSING_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError:
            # Do not doc.close() here: _run_processing is still running in its worker
            # thread and may be mid-call on this same doc, so closing it now would race
            # with that thread and risk a use-after-free crash in the native MuPDF layer.
            doc = None
            logging.error("PDF processing exceeded %s second timeout", PROCESSING_TIMEOUT_SECONDS)
            return func.HttpResponse(
                json.dumps({"error": "PDF processing timed out. Please try a smaller or simpler document."}),
                mimetype="application/json",
                status_code=500
            )
        finally:
            executor.shutdown(wait=False)

        return func.HttpResponse(
            output,
            mimetype="application/pdf",
            status_code=200,
            headers={
                "Content-Disposition": "attachment; filename=processed.pdf"
            }
        )

    except Exception:
        logging.exception("PDF processing error")
        return func.HttpResponse(
            json.dumps({"error": "PDF processing failed. Please check your input and try again."}),
            mimetype="application/json",
            status_code=500
        )

    finally:
        if doc:
            doc.close()
          
