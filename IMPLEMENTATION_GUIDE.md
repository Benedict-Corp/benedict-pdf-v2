# BenedictPDF v2.0 — Step-by-Step Implementation Guide

Replace all placeholder values (shown in `<angle brackets>`) with your actual values before running any command. A single leftover placeholder (e.g. `<tenant-id>`) will cause the command to fail — often with a confusing or unrelated-looking error — and can be genuinely difficult to troubleshoot without knowing that's the cause. **Before running a command, scan it once for any remaining `<...>` and replace it.**

---

## Part 1 — Deploy the Azure Function

### 1.1 Create the Function App

1. Go to https://portal.azure.com/
2. Click on "Create a resource"
3. Function App - Create
4. Hosting Option - Flex Consumption
5. Choose Your Subscription and Resource Group
6. Write function app name, e.g. "Benedict PDF"
7. Region - choose closest to your location
8. Runtime stack: Python
9. Version - 3.13
10. Instance size - 2048 MB or more depending on your needs
11. Go to Authentication tab and choose on Host Storage, Deployment Storage, and Application Insights Authentication type "Managed Identity". This avoids storing connection-string secrets for the function's access to its own supporting resources.

**Networking tab (during creation or in Function App settings):**
- Enable public access: **On** — the function needs to be reachable externally (Power Automate, direct API calls); access control is handled by Easy Auth (Part 2), not network isolation
- Enable virtual network integration: **Off** — not needed here; this function has no private backend resources to reach, and VNet integration adds cost/complexity with no benefit for this use case

**Note on VNet integration:** if your organization's policy requires VNet integration regardless of the above, be aware this implementation has been tested and verified only with public access and no VNet integration. VNet integration changes network routing (DNS resolution to Azure AD endpoints for Easy Auth token validation, outbound connectivity to Storage and Application Insights) in ways that have not been tested here. If deploying with VNet integration, treat it as a distinct configuration requiring its own verification — re-run all of Part 3's tests afterward — rather than an assumed drop-in change.

### 1.2 Build the function locally (Cloud Shell or local machine)

> **Shortcut:** the source files are also available directly in this repository — [`host.json`](host.json), [`requirements.txt`](requirements.txt), [`pdf_processor/__init__.py`](pdf_processor/__init__.py), [`pdf_processor/function.json`](pdf_processor/function.json). Download them, place them in the folder structure shown below, and skip ahead to [1.7 Deploy](#17-deploy).

Or continue below to build each file from scratch via Cloud Shell.

```bash
mkdir -p ~/BenedictPDF-v2/pdf_processor
```

### 1.3 host.json

```bash
cat > ~/BenedictPDF-v2/host.json << 'EOF'
{
  "version": "2.0",
  "functionTimeout": "00:01:00",
  "logging": {
    "applicationInsights": {
      "samplingSettings": {
        "isEnabled": true
      },
      "excludedTypes": "Request"
    }
  }
}
EOF
```

Note: `functionTimeout` is explicitly set to 1 minute, giving headroom above the function's own internal 20-second processing timeout (see 1.6), rather than relying on the platform default.

### 1.4 requirements.txt

```bash
cat > ~/BenedictPDF-v2/requirements.txt << 'EOF'
azure-functions==1.24.0
PyMuPDF==1.28.0
EOF
```

Confirm these versions are current and compatible before production deployment; pin to whatever version is actually tested if different.

### 1.5 pdf_processor/function.json

```bash
cat > ~/BenedictPDF-v2/pdf_processor/function.json << 'EOF'
{
  "bindings": [
    {
      "authLevel": "anonymous",
      "type": "httpTrigger",
      "direction": "in",
      "name": "req",
      "methods": ["post"]
    },
    {
      "type": "http",
      "direction": "out",
      "name": "$return"
    }
  ]
}
EOF
```

Note: `authLevel` is set to `anonymous` deliberately. Authentication is enforced separately at the platform level via Easy Auth (Part 2), rather than via a function key, to avoid two independent and inconsistent credential systems.

### 1.6 pdf_processor/__init__.py

```bash
cat > ~/BenedictPDF-v2/pdf_processor/__init__.py << 'EOF'
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
EOF
```

Note: the function accepts requests as **`multipart/form-data`** — the PDF arrives as a genuine file upload (`req.files`), not a base64-encoded JSON string. This matches how commercial PDF connectors (e.g. Encodian) work, and means the custom connector (Part 4) can expose `pdf` as a real file-type parameter — no manual `string()` wrapper needed on most trigger outputs. All other parameters (`action`, `watermark`, `password`, etc.) arrive as regular form fields. The function returns the processed PDF as **raw binary** (`application/pdf`) on success, and a JSON error object on failure (400/401/403/413/500).

#### How protected-document handling works (read before testing)

A PDF can carry two independent kinds of protection: a **user password** (required to open the file at all) and an **owner password** (grants full control — printing, copying, editing — independent of whether a user password exists). A document can have an owner password only, with no user password — meaning anyone can open and view it, but a compliant reader will restrict what they can do with it unless the owner password is supplied. This is a normal, common PDF pattern (e.g. "viewable by anyone, but not editable").

The function's behavior:

- **Any protected document** (requires a password to open, OR has any permission restriction even without needing a password) requires the caller to supply `current_password` in the request — even if the correct value is an empty string. Sending no `current_password` field at all is treated as "not attempting to authenticate" and is rejected; sending `current_password` as an explicit empty value is treated as a real authentication attempt and is checked against the document.
- **`action: "process"`** (watermarking) on a protected document additionally requires the caller to specify an output `password` — the function will not silently produce an unprotected output from a protected input.
- **`action: "remove_password"`** additionally requires that the supplied `current_password` grants **owner-level** access specifically, not just user-level (view) access. Knowing only the "anyone can view this" password is not sufficient to strip a document's protections.
- Documents with **no protection at all** are unaffected by any of the above and process exactly as before.

#### How granular permissions work (owner_password and allow_* fields)

By default, when you set an output `password`, it grants full access — anyone who opens the document with it can print, copy, and edit freely. To restrict specific permissions instead, supply a separate `owner_password` alongside optional `allow_*` fields:

- `owner_password` — a value distinct from `password`, required if you want to restrict anything. If you don't send it, the `allow_*` fields are ignored entirely and behavior is unchanged from a plain `password`-only request.
- `allow_printing`, `allow_high_res_printing`, `allow_copying`, `allow_editing`, `allow_annotations`, `allow_form_filling`, `allow_accessibility`, `allow_assembly` — each optional, each defaults to `true` (allowed) if not sent.

Two validation rules keep this feature from being used in a way that would silently do nothing or silently do the wrong thing:

- If `owner_password` is set but every `allow_*` field is left at its default (nothing actually restricted), the request is rejected — setting an owner password with no restrictions is almost certainly a mistake, not an intentional choice.
- If any `allow_*` field is explicitly sent as `false` but `owner_password` is not set, the request is rejected — a restriction request needs an owner password to mean anything; silently ignoring it would be a worse outcome than an explicit error.

**Important — a common mistake:** if `password` and `owner_password` are set to the *same* value, entering that password at the open prompt grants **full owner-level access and bypasses every restriction** — this is standard PDF behavior (any credential matching the owner password always grants owner rights), not a bug in this implementation. Use two genuinely different values if you want the restrictions to actually apply to whoever opens the document with the regular password.

### 1.7 Deploy

**Make sure to replace data in <...>!**

```bash
cd ~/BenedictPDF-v2 && zip -r ../BenedictPDF-v2.zip . && \
az functionapp deployment source config-zip \
  --resource-group <resource-group-name> \
  --name <function-app-name> \
  --src ../BenedictPDF-v2.zip
```

---

## Part 2 — Entra ID Authentication Setup

### 2.1 Register the app

**Make sure to replace data in <...>!**

1. **Entra ID** -> **App registrations** -> **New registration**
2. Name: `<app-name>`
3. Supported account types: **Accounts in this organizational directory only** (single tenant)
4. Redirect URI: leave blank for now
5. **Register**
6. Record the **Application (client) ID** and **Directory (tenant) ID** from the Overview page

### 2.2 Create a client secret
On created App Registration page find "Certificates & secrets" on Manage tab.
Choose "Client Secrets":

1. Click "New client secret"
2. Description: choose your own or write "Benedict PDF"
3. Expires - please note that you will need to remake it and change client secret everywhere after it expired, so choose wisely.
4. Click "Add"

**!!!Note: Save your client secret value since it is shown only once. It's not possible to retrieve it later.**

**(OPTIONAL)** If the portal blocks automatic secret creation (tenant policy), use CLI instead:

```bash
az ad app credential reset --id <client-id> --append --display-name "<secret-label>"
```

This returns `appId`, `password` (the secret — record it immediately, it is shown once only), and `tenant`.

### 2.3 Expose an API

**Make sure to replace data in <...>!**

1. App registration -> **Expose an API** -> **Add a scope**
2. Accept the default Application ID URI (`api://<client-id>`) - this is usually written by default according to client-id of this app. Confirm it and click "Save and continue"
3. Add scope: name `access_as_user`, consentable by Admins and users

### 2.4 Add required Graph permission

1. App registration -> **API permissions** -> **Add a permission**
2. **Microsoft Graph** -> **Delegated permissions** -> `User.Read` (Note: this is usually enabled by default)
3. **Grant admin consent** for the tenant

### 2.5 Enable Easy Auth on the Function App

1. Function App -> **Authentication** -> **Add identity provider**
2. Identity provider: **Microsoft**
3. Choose a tenant for your application and its users: **Workforce configuration (current tenant)**
4. App registration type: **Provide the details of an existing app registration**
5. Application (client) ID: `<client-id>` from step 2.1
6. Client secret: leave blank (not required for token validation)
7. **Issuer URL — set this proactively, do not leave the default.** The portal's default issuer URL points to the v2.0 endpoint (`.../v2.0`), but app registrations created this way typically issue v1.0-format tokens. If left as the default, authenticated calls will fail with an audience validation error. Set it to:
   ```
   https://sts.windows.net/<tenant-id>/
   ```
   (no `/v2.0` suffix). Confirm by decoding a real token later (Part 4) and checking its `ver` and `iss` claims match.
7. Restrict access: **Require authentication**
8. Unauthenticated requests: **HTTP 401 Unauthorized**
9. **Add**

### 2.6 Configure allowed audiences directly

**Make sure to replace data in <...>! Read the whole code carefully and replace <...> everywhere, otherwise function will be failing!**

The portal UI does not always allow granular audience configuration. Set it via the REST API directly, including both audience forms (with and without the `api://` prefix — different token flows produce different formats):

```bash
az rest --method PUT \
  --uri "https://management.azure.com/subscriptions/<subscription-id>/resourceGroups/<resource-group-name>/providers/Microsoft.Web/sites/<function-app-name>/config/authsettingsV2?api-version=2022-03-01" \
  --body '{
    "properties": {
      "clearInboundClaimsMapping": "false",
      "globalValidation": {
        "excludedPaths": [],
        "requireAuthentication": true,
        "unauthenticatedClientAction": "Return401"
      },
      "httpSettings": {
        "forwardProxy": { "convention": "NoProxy" },
        "requireHttps": true,
        "routes": { "apiPrefix": "/.auth" }
      },
      "identityProviders": {
        "azureActiveDirectory": {
          "enabled": true,
          "login": { "disableWWWAuthenticate": false },
          "registration": {
            "clientId": "<client-id>",
            "openIdIssuer": "https://sts.windows.net/<tenant-id>/"
          },
          "validation": {
            "allowedAudiences": [
              "api://<client-id>",
              "<client-id>"
            ],
            "defaultAuthorizationPolicy": {
              "allowedApplications": ["<client-id>"],
              "allowedPrincipals": {}
            },
            "jwtClaimChecks": {}
          }
        }
      },
      "login": {
        "cookieExpiration": { "convention": "FixedTime", "timeToExpiration": "08:00:00" },
        "nonce": { "nonceExpirationInterval": "00:05:00", "validateNonce": true },
        "preserveUrlFragmentsForLogins": false,
        "routes": {},
        "tokenStore": {
          "azureBlobStorage": {},
          "enabled": true,
          "fileSystem": {},
          "tokenRefreshExtensionHours": 72.0
        }
      },
      "platform": { "enabled": true, "runtimeVersion": "~1" }
    }
  }'
```

Note: this endpoint requires PUT (full replace), not PATCH. Include all existing configuration in the body, not just the fields being changed, or other settings will be reset to defaults.

### 2.7 Restrict access to explicitly assigned users (recommended)

By default, any authenticated user in the tenant can obtain a valid token for this app and access the function. To restrict access to specific individuals:

1. **Entra ID** -> **Enterprise applications** -> find the app
2. **Properties** -> **Assignment required?** -> **Yes** -> Save
3. **Users and groups** -> **Add user/group** -> add each authorized user (or a security group, for easier ongoing management)

Once enabled, unassigned users are blocked at sign-in with error `AADSTS50105`, before a token is ever issued.

**Important — this affects app-to-app (client credentials) calls differently than user sign-in.** With "Assignment required" enabled, service-to-service calls using the client credentials flow (client ID + secret, no user involved) will fail with `AADSTS501051` ("Application is not assigned to a role") unless the app is explicitly granted an application-type role on itself. This is a separate step from assigning individual users:

1. App registrations -> **App roles** -> **Create app role**
   - Display name: `PDF.ServiceAccess`
   - Allowed member types: **Applications**
   - Value: `PDF.ServiceAccess`
   - Enable it
2. Ensure a service principal exists for the app (it may not be created automatically):
   ```bash
   az ad sp create --id <client-id>
   ```
   (Safe to run even if one already exists — will no-op or return the existing one. You will just receive an error that some SP already in use.)
3. Assign the app role to the app's own service principal:
   ```bash
   SP_ID=$(az ad sp show --id <client-id> --query id -o tsv)

   az rest --method POST \
     --uri "https://graph.microsoft.com/v1.0/servicePrincipals/$SP_ID/appRoleAssignedTo" \
     --body "{
       \"principalId\": \"$SP_ID\",
       \"resourceId\": \"$SP_ID\",
       \"appRoleId\": \"<app-role-id-from-step-1>\"
     }"
   ```
   Get `<app-role-id-from-step-1>` via:
   ```bash
   az ad app show --id <client-id> --query "appRoles[].{id:id, value:value}" -o table
   ```

### 2.8 Additional platform hardening (recommended, quick settings)

These are standard defense-in-depth settings, none required for the function to work correctly, but cheap to apply and worth doing:

1. **HTTPS Only.** Function App -> **Configuration** -> **General settings** -> **HTTPS Only** -> **On** -> Save.
2. **Minimum TLS Version.** Same tab -> **Minimum TLS Version** -> **1.2** -> Save.
3. **CORS.** Function App -> **CORS** -> remove any existing entries (including any wildcard `*`) -> leave the allowed origins list empty -> Save. This function is called server-to-server (not from a browser), so CORS does not need to allow anything.
4. **Storage account key access.** Go to the storage account backing this Function App (visible as a linked resource on the Function App's Overview page, or in the same resource group) -> **Configuration** -> **Allow storage account key access** -> **Disabled** -> Save. This is safe because the function authenticates to storage via Managed Identity (configured in 1.1), not a connection-string key. Before disabling, confirm nothing else depends on key-based access to this storage account.

As of recent Azure defaults, items 1-4 may already be set correctly on newly created resources — check current values before assuming changes are needed.

---

## Part 3 — Verification

### 3.1 Confirm unauthenticated requests are blocked

```bash
curl -i -X POST "https://<function-app-name>.azurewebsites.net/api/pdf_processor" \
  -H "Content-Type: application/json" \
  -d '{"pdf": "test"}'
```
#### **Note:** you may find function URL by going to 1. Function 2. click on pdf_processor 3. on "Code + Test" find "Get function URL" 4. copy any of them and replace https://<function-app-name>.azurewebsites.net/api/pdf_processor with it.

Expected: `HTTP/1.1 401 Unauthorized`

### 3.2 Obtain a token (client credentials flow)

```bash
TOKEN=$(curl -s -X POST https://login.microsoftonline.com/<tenant-id>/oauth2/v2.0/token \
  -d "client_id=<client-id>" \
  -d "client_secret=<client-secret>" \
  -d "scope=api://<client-id>/.default" \
  -d "grant_type=client_credentials" | jq -r .access_token)

echo $TOKEN | cut -c1-10
```

Should print `eyJ...`. If it returns `null`, run the request without the `jq` filter to see the raw error response — most commonly `AADSTS501051` (see Part 2.7's app role fix) or an audience/issuer misconfiguration.

### 3.3 Confirm authenticated requests succeed

Requests are `multipart/form-data` — the PDF is sent as a real file, not base64 text:

```bash
curl -s -o /tmp/response.pdf -w "HTTP Status: %{http_code}\n" -X POST "<FUNCTION_URL_FROM_STEP_3.1>" \
  -H "Authorization: Bearer $TOKEN" \
  -F "pdf=@<path-to-test.pdf>;type=application/pdf" \
  -F "action=process" \
  -F "watermark=TEST"
```

#### Expected: `HTTP Status: 200`. Since the response is raw binary on success, `-o` alone is sufficient here — no need to avoid combining flags the way the old JSON-based version did.

Verify the saved file is a genuine, valid PDF:
```bash
file /tmp/response.pdf
```

### 3.4 Confirm oversized payloads are rejected

```bash
head -c 30000000 /dev/urandom > /tmp/big_payload.bin

curl -i -X POST "<FUNCTION_URL_FROM_STEP_3.1>" \
  -H "Authorization: Bearer $TOKEN" \
  -F "pdf=@/tmp/big_payload.bin;type=application/pdf" \
  -F "action=process"
```

Expected: `HTTP/1.1 413` with `"File exceeds maximum size of 25 MB"`.

### 3.5 Confirm assignment restriction (if configured per 2.7)

Have a non-assigned test account attempt to sign in via the intended client application. Expected error: `AADSTS50105`. After assigning that account, retry — sign-in and function access should now succeed.

#### Note: we recommend trying it after creating the custom connector in Power Automate (see below).

### 3.6 Confirm input-validation edge cases

```bash
# Non-string pdf field (sent as a plain form value rather than a file part)
curl -s -o /tmp/resp_badtype.pdf -w "HTTP Status: %{http_code}\n" -X POST "<FUNCTION_URL_FROM_STEP_3.1>" \
  -H "Authorization: Bearer $TOKEN" -F "pdf=12345" -F "action=process"
cat /tmp/resp_badtype.pdf
```
Expected: `400` with an error indicating the `pdf` field is invalid.

### 3.7 Confirm protected-document handling (see "How protected-document handling works" in 1.6)

Create a test PDF with an owner password only (viewable by anyone, restricted permissions):

```bash
pip install PyMuPDF==1.28.0 --user --quiet   # or use a venv if this fails on permissions

python3 -c "
import fitz
doc = fitz.open('<path-to-any-test.pdf>')
doc.save('/tmp/owner_protected.pdf', encryption=fitz.PDF_ENCRYPT_AES_256, user_pw='', owner_pw='realsecret123', permissions=fitz.PDF_PERM_ACCESSIBILITY)
doc.close()
"
```

Test 1 — no `current_password` supplied, expect `400`:
```bash
curl -s -o /tmp/resp1.pdf -w "%{http_code}\n" -X POST "<FUNCTION_URL_FROM_STEP_3.1>" \
  -H "Authorization: Bearer $TOKEN" -F "pdf=@/tmp/owner_protected.pdf;type=application/pdf" -F "action=process" -F "watermark=TEST"
```

Test 2 — empty `current_password`, `process`, no output password, expect `400` (must specify output protection):
```bash
curl -s -o /tmp/resp2.pdf -w "%{http_code}\n" -X POST "<FUNCTION_URL_FROM_STEP_3.1>" \
  -H "Authorization: Bearer $TOKEN" -F "pdf=@/tmp/owner_protected.pdf;type=application/pdf" -F "action=process" -F "watermark=TEST" -F "current_password="
```

Test 3 — empty `current_password`, `process`, with output password, expect `200`:
```bash
curl -s -o /tmp/resp3.pdf -w "%{http_code}\n" -X POST "<FUNCTION_URL_FROM_STEP_3.1>" \
  -H "Authorization: Bearer $TOKEN" -F "pdf=@/tmp/owner_protected.pdf;type=application/pdf" -F "action=process" -F "watermark=TEST" -F "current_password=" -F "password=newoutputpassword123"
```

Test 4 — empty `current_password`, `remove_password`, expect `403` (owner-level required, empty password only proves user-level access on this document):
```bash
curl -s -o /tmp/resp4.pdf -w "%{http_code}\n" -X POST "<FUNCTION_URL_FROM_STEP_3.1>" \
  -H "Authorization: Bearer $TOKEN" -F "pdf=@/tmp/owner_protected.pdf;type=application/pdf" -F "action=remove_password" -F "current_password="
```

Expected results: `400`, `400`, `200`, `403` in that order.

### 3.8 Confirm granular permission restrictions (owner_password)

Test A — request a restriction without `owner_password`, expect `400`:
```bash
curl -s -o /tmp/resp_perm_a.pdf -w "%{http_code}\n" -X POST "<FUNCTION_URL_FROM_STEP_3.1>" \
  -H "Authorization: Bearer $TOKEN" -F "pdf=@<path-to-test.pdf>;type=application/pdf" -F "action=process" -F "allow_printing=false"
```

Test B — `owner_password` set, nothing restricted, expect `400`:
```bash
curl -s -o /tmp/resp_perm_b.pdf -w "%{http_code}\n" -X POST "<FUNCTION_URL_FROM_STEP_3.1>" \
  -H "Authorization: Bearer $TOKEN" -F "pdf=@<path-to-test.pdf>;type=application/pdf" -F "action=process" -F "owner_password=ownersecret123"
```

Test C — `owner_password` set, printing and copying restricted, expect `200`:
```bash
curl -s -o /tmp/resp_perm_c.pdf -w "%{http_code}\n" -X POST "<FUNCTION_URL_FROM_STEP_3.1>" \
  -H "Authorization: Bearer $TOKEN" -F "pdf=@<path-to-test.pdf>;type=application/pdf" -F "action=process" \
  -F "owner_password=ownersecret123" -F "allow_printing=false" -F "allow_copying=false"
```

Expected results: `400`, `400`, `200` in that order. For Test C, verify by opening `/tmp/resp_perm_c.pdf` and confirming print/copy are disabled while other actions remain available (or authenticate with `ownersecret123` to confirm full access is still available at the owner level).

---

## Part 4 — Custom Connector Setup (Power Automate)

### 4.1 Add a Web redirect platform to the app registration

1. App registration -> **Authentication (Preview)** -> **Add Redirect URL** -> **Web**
2. Redirect URI: `https://global.consent.azure-apim.net/redirect`
3. Implicit grant checkboxes ("Access tokens", "ID tokens"): leave both **unchecked** — the connector uses authorization code flow, not implicit flow
4. Save

**A second, connector-specific redirect URI will be required after the connector is created (step 4.4).**

### 4.2 Create the connector

1. **make.powerautomate.com** -> **Data** -> **Custom connectors** -> **New custom connector** -> **Create from blank**
2. Connector name: for example "Benedict PDF"
2. General tab: Host = `<function-app-name>.azurewebsites.net`, Scheme = HTTPS

#### For "Host" go to 1. Function 2. Find "Default domain" on Overview page 3. Copy and replace <function-app-name>.azurewebsites.net

### 4.3 Security tab

- Authentication type: **OAuth 2.0**
- Identity provider: **Azure Active Directory**
- Client ID: `<client-id>`
- Client secret: `<client-secret>` **Client secret will not be visible to anyone openning the custom connector, but you will need to paste it each time you make changes in Security tab**
- Authorization URL: `https://login.microsoftonline.com`
- Tenant ID: `<tenant-id>`
- **Resource URL: `<client-id>`** (bare GUID, not `api://<client-id>` — required when client and resource are the same application under the v1.0 endpoint; using the `api://` form here causes `AADSTS90009`)
- Scope: leave blank
- Enable on-behalf-of login: keep false

### 4.4 Add the connector-specific redirect URI

After saving the Security tab, note the exact **Redirect URL** displayed — it includes a connector-instance-specific suffix, e.g.:
```
https://global.consent.azure-apim.net/redirect/<connector-specific-hash>
```
Add this **exact** URI (copy it precisely) to the app registration's Authentication -> Web platform redirect URIs. The generic `https://global.consent.azure-apim.net/redirect` from step 4.1 alone is not sufficient — connections will fail with `AADSTS50011` (redirect URI mismatch) until this specific URI is also added.

1. Click "Edit" under "Redirect URI" and **replace with Redirect URL from Power Automate**
2. Click configure

### 4.5 Definition tab — request and response schema

**Make sure to replace data in <...>!**

The request format is `multipart/form-data` with the PDF as a genuine file parameter, matching how commercial connectors like Encodian work. This must be built in **Code view** (the Swagger/OpenAPI editor) rather than the UI's guided "Import from sample" flow, since the UI's sample-import tool is oriented around JSON bodies and doesn't cleanly generate `formData`/file parameters.

1. Open the connector -> **Definition** tab -> **Code view**
2. Replace the entire contents with the following (update `host:` to your actual function app's hostname, and `<your-tenant-id>` in `securityDefinitions` to your real tenant ID):

```yaml
swagger: '2.0'
info:
  title: Benedict PDF
  description: ''
  version: '1.0'
host: <function-app-name>.azurewebsites.net
basePath: /
schemes:
  - https
consumes:
  - multipart/form-data
produces:
  - application/json
  - application/pdf
paths:
  /api/pdf_processor:
    post:
      responses:
        '200':
          description: Processed PDF file
          schema:
            type: string
            format: binary
          headers:
            Content-Disposition:
              type: string
              description: Attachment filename
        default:
          description: Error response
          schema:
            type: object
            properties:
              error:
                type: string
                description: Error message
      operationId: BenedictPDF
      summary: Process PDF
      description: Secure PDF or remove password protection
      parameters:
        - name: pdf
          in: formData
          required: true
          type: file
          x-ms-summary: File
          description: The PDF file to process
        - name: action
          in: formData
          required: true
          type: string
          x-ms-summary: Action
          description: What to do with the document
          enum:
            - process
            - remove_password
        - name: watermark
          in: formData
          required: false
          type: string
          x-ms-summary: Watermark text
          description: Text to stamp onto the document (leave blank for no watermark)
        - name: password
          in: formData
          required: false
          type: string
          x-ms-summary: Open password
          description: Password required to open the document. Leave blank for no password.
        - name: current_password
          in: formData
          required: false
          type: string
          x-ms-summary: Current password
          description: The document's existing password, if it's already protected. Required to process or unlock a protected document, even if the existing password is blank.
        - name: color
          in: formData
          required: false
          type: string
          x-ms-summary: Watermark color
          description: Hex color code for the watermark text
          default: 4C5270
        - name: fontsize
          in: formData
          required: false
          type: integer
          format: int32
          x-ms-summary: Watermark font size
          description: Font size for the watermark text
        - name: padding_top
          in: formData
          required: false
          type: integer
          format: int32
          x-ms-summary: Top padding
          description: Space from the top edge, in points
        - name: padding_right
          in: formData
          required: false
          type: integer
          format: int32
          x-ms-summary: Side padding
          description: Space from the left or right edge (depending on position), in points
        - name: position
          in: formData
          required: false
          type: string
          x-ms-summary: Watermark position
          description: Where to place the watermark on the page
          enum:
            - top-right
            - top-left
            - bottom-right
            - bottom-left
            - center
        - name: first_page_only
          in: formData
          required: false
          type: boolean
          x-ms-summary: Watermark first page only
          description: If true, only stamp the watermark on the first page
        - name: owner_password
          in: formData
          required: false
          type: string
          x-ms-summary: Owner (permissions) password
          description: Sets a separate password that grants full control over the document. Required if you're restricting any permissions below -- setting a restriction without this will return an error.
        - name: allow_printing
          in: formData
          required: false
          type: boolean
          x-ms-summary: Allow printing
          description: Requires owner_password. Setting this to false without owner_password returns an error.
        - name: allow_high_res_printing
          in: formData
          required: false
          type: boolean
          x-ms-summary: Allow high-resolution printing
          description: Requires owner_password. Setting this to false without owner_password returns an error.
        - name: allow_copying
          in: formData
          required: false
          type: boolean
          x-ms-summary: Allow copying
          description: Requires owner_password. Setting this to false without owner_password returns an error.
        - name: allow_editing
          in: formData
          required: false
          type: boolean
          x-ms-summary: Allow editing
          description: Requires owner_password. Setting this to false without owner_password returns an error.
        - name: allow_annotations
          in: formData
          required: false
          type: boolean
          x-ms-summary: Allow annotations
          description: Requires owner_password. Setting this to false without owner_password returns an error.
        - name: allow_form_filling
          in: formData
          required: false
          type: boolean
          x-ms-summary: Allow form filling
          description: Requires owner_password. Setting this to false without owner_password returns an error.
        - name: allow_accessibility
          in: formData
          required: false
          type: boolean
          x-ms-summary: Allow accessibility extraction
          description: Requires owner_password. Setting this to false without owner_password returns an error.
        - name: allow_assembly
          in: formData
          required: false
          type: boolean
          x-ms-summary: Allow page assembly
          description: Allows inserting, deleting, or rotating pages. Requires owner_password. Setting this to false without owner_password returns an error.
definitions: {}
parameters: {}
security:
  - oauth2-auth: []
tags: []
securityDefinitions:
  oauth2-auth:
    type: oauth2
    flow: accessCode
    tokenUrl: https://login.microsoftonline.com/<your-tenant-id>/oauth2/v2.0/token
    scopes: {}
    authorizationUrl: https://login.microsoftonline.com/<your-tenant-id>/oauth2/v2.0/authorize
```

3. Save/**Update connector**

Note: `securityDefinitions.oauth2-auth.tokenUrl`/`authorizationUrl` may display using the generic `login.windows.net/common` endpoint after saving, regardless of what you enter here or what your Security tab's Tenant ID field shows — this appears to be how Power Automate's connector platform generates this specific field and does not indicate a real tenant-scoping problem. Actual tenant-scoping is controlled by the Security tab's Client ID / Tenant ID fields (Step 4.3), which do work correctly; verify by testing sign-in and a real request rather than relying on what's shown in this field.

**If the connector's Test tab or a flow doesn't reflect a change you just saved** (e.g. a parameter you added doesn't appear): try a hard refresh of the browser tab, or removing and re-adding the action in your flow. If neither works within a few minutes, it may be backend propagation delay — this has been observed to occasionally take longer than expected.

**Missing `action` or `position` after pasting Swagger:** if you're editing an existing connector rather than pasting the full block above fresh, double-check both parameters are actually present after saving — they're easy to accidentally drop during manual edits, and the connector will otherwise silently default `action` to `process` (making `remove_password` unreachable) with no error telling you why.

### 4.6 Create a connection and test

1. Save the connector -> **Test** tab -> **New connection**
2. Sign in (this triggers the authorization code flow via the redirect URIs configured above)
3. Run the test action with a real base64 PDF and valid parameters

Once working, the action's output in a flow will expose:
- **Body** — the raw PDF file content, directly usable as a file (e.g. wired straight into an Outlook/SharePoint)
- **headers/Content-Disposition** — the filename metadata

---

## Part 5 — Known Limitations

**PDFs with an owner password that grants full permissions are indistinguishable from unprotected PDFs.**
If a document was deliberately owner-password-protected but every permission was left allowed (an unusual but valid authoring choice), it reports the same `needs_pass`/`permissions` values as a genuinely unprotected document. There is no way to distinguish these two cases via PyMuPDF's API without attempting authentication unconditionally on every document, which is not implemented. This is a narrow, disclosed edge case, not an open vulnerability — it does not allow bypassing protection on any document that actually restricts anything.

**Setting `password` and `owner_password` to the same value silently defeats permission restrictions.**
Per PDF specification, any credential matching the owner password always grants full owner-level access, regardless of the permission bits requested. If both fields are set to the same value, whoever opens the document with that password gets full access, not the restricted access intended by the `allow_*` fields. This is correct PDF behavior, not a bug, but it's an easy mistake to make. The function does not currently validate that the two values differ — consider adding this check at the calling application/UI layer if you build one, since it's a usability concern rather than a security one (nothing is bypassed that a genuine owner shouldn't be able to bypass).

**VNet integration has not been tested.** See the note in Part 1.1.

---

## Part 6 — Recommendations

**Security/cleanup:**
- Rotate/remove any test-only client secrets; retain only the current production secret
- Review and remove test accounts from the Enterprise Application's assigned users list before production use
