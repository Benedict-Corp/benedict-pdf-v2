# BenedictPDF v2

![Benedict Corp](https://github.com/Benedict-Corp/benedict-pdf-v2/blob/main/assets/logo.csv)

**Hardened, Entra ID-authenticated PDF processing for Microsoft Power Platform — watermark, password protect, restrict permissions, and remove protection on your own Azure infrastructure.**

[![License: MIT + Commons Clause](https://img.shields.io/badge/License-MIT%20%2B%20Commons%20Clause-blue.svg)](LICENSE)
[![Open Source](https://img.shields.io/badge/Open%20Source-Yes-brightgreen.svg)](https://github.com/Benedict-Corp/benedict-pdf-v2)
[![Power Platform](https://img.shields.io/badge/Microsoft-Power%20Platform-742774.svg)](https://www.microsoft.com/en-us/power-platform)

[Website](https://www.benedictcorp.com/) · [Support the Project](https://buy.stripe.com/00w3cx79a1N7cT7c1qcAo07) · [Report an Issue](https://github.com/Benedict-Corp/benedict-pdf-v2/issues)

---

## Overview

BenedictPDF v2 is the hardened, production-grade evolution of [BenedictPDF](https://github.com/Benedict-Corp/benedict-pdf). It runs as an Azure Function inside your own Azure subscription — your documents never leave your Microsoft environment.

**What it does:**
- Add text watermarks to PDFs with full control over position, color, size, and padding
- Password protect PDFs with AES-256 encryption
- Restrict specific permissions (printing, copying, editing, annotations, and more) independently of the password required to open the document
- Remove passwords and permission restrictions from PDFs, with owner-level credential verification

**v2 vs v1 — what's different:**

| | v1 | v2 |
|---|---|---|
| Authentication | Shared function key | Entra ID (Azure AD), per-user assignment |
| Request size / page limits | None | Enforced |
| Rate/timeout protection | None | Processing timeout + page-count ceiling |
| Granular permissions (restrict printing/copying/editing) | ❌ | ✅ |
| Owner-vs-user password distinction on removal | ❌ | ✅ Verified before allowing removal |
| Sample Power App included | ✅ | ❌ (function + connector only — see [Why no sample app](#why-no-sample-app)) |
| DocuSign / e-signature integration | ✅ | ❌ (not yet ported) |
| Deployment complexity | Low — copy/paste and go | Higher — requires Entra ID app registration |

**If you want the fastest path to something working, or don't need identity-based access control, [BenedictPDF v1](https://github.com/Benedict-Corp/benedict-pdf) is the better starting point.** Come back to v2 when you need real access control over who can call the function, or when you need to restrict what recipients can do with a document beyond just requiring a password to open it.

---

## Why No Sample App

v1 ships a Power App and Power Automate solution you can import and run in minutes, because its authentication (a shared function key) is the same for everyone and requires no per-deployer setup.

v2's authentication is fundamentally different: it requires **you** to register an application in **your own** Entra ID tenant, configure Easy Auth on your Function App, and build a custom connector referencing your own tenant's client ID and secret. None of that can be packaged into a reusable sample app — a sample app's connector configuration would reference *our* tenant, not yours, and simply wouldn't work once imported.

What v2 ships instead is the [**Implementation Guide**](IMPLEMENTATION_GUIDE.md) — a complete, tested, step-by-step walkthrough for deploying the function, setting up Entra ID authentication, and building your own custom connector from scratch. Once you've built your connector following the guide, wiring it into a Power App or Power Automate flow works exactly like any other connector.

---

## Prerequisites

- **Microsoft 365** with Power Platform access
- **Azure subscription**
- **Microsoft Power Automate Premium or Power Apps Premium license** — required for custom connectors
- Comfort with the Azure Portal and Cloud Shell — v2's setup involves more steps than v1's copy/paste deployment

---

## Getting Started

Deployment, Entra ID setup, and custom connector configuration are fully documented in the **[Implementation Guide](IMPLEMENTATION_GUIDE.md)**. It covers, in order:

1. Deploying the Azure Function
2. Registering an app in Entra ID and configuring Easy Auth
3. Restricting access to specific assigned users
4. Verifying the deployment (a full set of test commands, including the permission-restriction feature)
5. Building the custom connector
6. Known limitations

Follow it start to finish — it's been tested end-to-end, including a full from-scratch rebuild, so each step should work as written.

---

## Security

v2 was built specifically to close the gaps documented in [v1's Security section](https://github.com/Benedict-Corp/benedict-pdf#security):

- **Entra ID authentication**, enforced at the Azure platform level before any request reaches the function code. Access can be restricted to specific assigned users, not just anyone who has the URL and key.
- **Request size and page-count limits**, checked before expensive processing begins.
- **A hard processing timeout**, so a maliciously crafted or pathological PDF can't tie up compute indefinitely.
- **Granular output permissions.** Setting an `owner_password` lets you restrict specific permissions (printing, copying, editing, annotations, form-filling, accessibility extraction, page assembly) independently of the password required to open the document — something v1 cannot do at all.
- **Owner-level credential verification on removal.** Stripping a document's protections requires proving owner-level access, not just any valid password — closing a real vulnerability class where a document restricted only at the permissions level (no open password) could previously have its protections silently removed by anyone.
- **Explicit, fail-loud validation** — ambiguous or contradictory requests (e.g. requesting a permission restriction without an owner password) are rejected with a clear error rather than silently ignored or silently applied incorrectly.

**What v2 deliberately still does not include, with reasoning:**
- **Parser-level sandboxing/process isolation.** Given Entra ID authentication and per-user assignment restrict who can call the function to known, individually authenticated people, full sandboxing is disproportionate complexity for this tool's realistic threat model. Worth revisiting if usage patterns change.
- **A JavaScript/embedded-content sanitization pass.** PDFs can carry embedded scripts; v2 doesn't currently scan for or strip them. This is on the roadmap as a possible future feature (see [Ideas / Roadmap](#ideas--roadmap)), not something you should assume is already handled.
- **VNet integration.** Tested and documented as public-access only — see the Implementation Guide's note on this before deploying into a VNet-restricted environment.

---

## Power Automate — Connector Parameters

Once you've built your custom connector per the Implementation Guide, it exposes the following fields:

| Parameter | Description | Default |
|---|---|---|
| `pdf` | The file to process (true file upload, not base64 text) | Required |
| `action` | `process` or `remove_password` | Required |
| `watermark` | Watermark text | None |
| `password` | Password required to open the document | None |
| `current_password` | The document's existing password, if already protected — required even if the existing password is blank | None |
| `color` | Watermark color, hex without `#` | `4C5270` |
| `fontsize` | Watermark font size | `7` |
| `padding_top` | Top padding, in points | `5` |
| `padding_right` | Left/right padding (depending on position), in points | `0` |
| `position` | `top-right`, `top-left`, `bottom-right`, `bottom-left`, `center` | `top-right` |
| `first_page_only` | Watermark only the first page | `false` |
| `owner_password` | A separate password granting full control. Required if restricting any permission below. | None |
| `allow_printing` | Allow printing. Requires `owner_password`; setting `false` without it returns an error. | `true` |
| `allow_high_res_printing` | Allow high-resolution printing. Same requirement as above. | `true` |
| `allow_copying` | Allow copying/extracting content. Same requirement as above. | `true` |
| `allow_editing` | Allow editing/modifying. Same requirement as above. | `true` |
| `allow_annotations` | Allow annotations/comments. Same requirement as above. | `true` |
| `allow_form_filling` | Allow form filling. Same requirement as above. | `true` |
| `allow_accessibility` | Allow accessibility content extraction. Same requirement as above. | `true` |
| `allow_assembly` | Allow inserting, deleting, or rotating pages. Same requirement as above. | `true` |

**Example — restrict printing and copying, everything else allowed:**

Set `owner_password` to a strong, unique value, `allow_printing` to `false`, `allow_copying` to `false`, leave the rest at their defaults. If you also set `password`, the document requires that password to open at all; if you leave `password` blank, the document opens for anyone but the restrictions still apply unless someone authenticates with `owner_password`.

> **Important:** if `password` and `owner_password` are set to the *same* value, entering that password grants full owner-level access and bypasses all restrictions — this is standard PDF behavior, not a bug. Use two genuinely different values if you want the restrictions to actually apply to whoever opens the document with the regular password.

---

## Known Limitations

**PDFs with an owner password that grants full permissions are indistinguishable from unprotected PDFs.** If a document was deliberately owner-password-protected but every permission was left allowed, it's not detectably different from a genuinely unprotected document. This is a narrow, disclosed edge case — it does not allow bypassing protection on any document that actually restricts something.

**VNet integration has not been tested.** See the Implementation Guide for details before deploying into a VNet-restricted environment.

Full details on all of these are in the [Implementation Guide](IMPLEMENTATION GUIDE.md).

---

## Ideas / Roadmap

Not built yet, under consideration:
- A `sanitize` action to strip embedded JavaScript/scripts from incoming PDFs - useful for organizations wanting to inspect external email attachments before they're opened
- Redaction support (true content removal)

If either of these would be useful to you, [open an issue](https://github.com/Benedict-Corp/benedict-pdf-v2/issues) - real interest helps prioritize what gets built next.

---

## Known Issues & Troubleshooting

| Issue | Cause | Fix |
|---|---|---|
| `HTTP 401` on every request | Missing or invalid Entra ID token | Confirm your connector's OAuth connection is signed in and the token hasn't expired |
| `AADSTS50105` at sign-in | Your account isn't assigned access to the app registration | Have an admin assign your account under Enterprise Applications → Users and groups |
| `AADSTS501051` on client-credentials calls | The calling app isn't assigned an app role | See the Implementation Guide's app role section |
| `Document is protected. Provide current_password...` on a document you didn't think was protected | The document has an owner password with restricted permissions, even though it opens without a prompt | Supply `current_password` (an empty string if the document's real open-password is blank) |
| Restriction request rejected with an error naming `owner_password` | You set an `allow_*` field to `false` without also setting `owner_password` | Set `owner_password`, or remove the restriction fields |
| `owner_password was provided but no permissions were restricted` | You set `owner_password` without setting any `allow_*` field to `false` | Either restrict at least one permission, or omit `owner_password` |

If you encounter an issue not listed here, please [open an issue on GitHub](https://github.com/Benedict-Corp/benedict-pdf-v2/issues) with details of your environment and the error message.

**Before opening an issue or sharing files:** if you're attaching an exported solution, flow definition, or connector configuration, search it for your client secret/tenant ID first and redact it. See the [Security](#security) section and the Implementation Guide for a documented case where secrets can end up embedded in exports even with export-value protections switched off.

---

## License

BenedictPDF is released under the MIT License with Commons Clause.

You are free to use, modify, and integrate BenedictPDF into your own products and workflows. You may not resell BenedictPDF or a minimally modified version of it as a standalone product or service.

See [LICENSE](https://github.com/Benedict-Corp/benedict-pdf-v2/blob/main/LICENSE) for full details.

---

## Support

BenedictPDF is free and will stay free.

If it saves you time or money, consider supporting Benedict Corp so we can keep building tools like this.

[**Support via Stripe →**](https://buy.stripe.com/00w3cx79a1N7cT7c1qcAo07)

For questions or issues: [github.com/Benedict-Corp/benedict-pdf-v2/issues](https://github.com/Benedict-Corp/benedict-pdf-v2/issues)

For custom Power Platform solutions built for your organization: [benedictcorp.com](https://www.benedictcorp.com/) or email us at info@benedictcorp.com

---

*Benedict Corp. is not affiliated with or endorsed by Microsoft Corporation. Azure, Power Automate, Power Apps, and Logic Apps are trademarks of Microsoft Corporation. Stripe is a trademark of Stripe, Inc.*
