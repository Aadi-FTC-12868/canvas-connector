# Hosted beta notice and local setup

The Built by Aadi account flow displays this notice after sign-in and immediately
above the Canvas connection form. The public, account-free local installation
page is `https://builtbyaadi.com/canvas-connector/local.html`.

## Notice

**This hosted connector is in beta.** Bugs, outages, or unauthorized access could
expose your Canvas data. Use it at your own risk; security and availability are
not guaranteed.

We strongly recommend [local setup](https://builtbyaadi.com/canvas-connector/local.html)
if you prefer to keep your Canvas token on your own computer. Local setup gives
you more control, but still depends on your device and configuration.

To the extent permitted by applicable law, Built by Aadi disclaims warranties
and liability for loss, outages, or unauthorized disclosure arising from use of
this beta. This notice does not exclude rights or liabilities that cannot legally
be excluded.

## Credential explanation

The hosted implementation stores the Canvas token encrypted in the operator's
Cloudflare infrastructure. It sends the token to the configured Canvas server
to authenticate requests; it does not provide the token to ChatGPT or Claude.
The assistant does receive Canvas content requested through the connector.
Do not describe this as a guarantee that a breach is impossible, or say the token
literally never leaves Cloudflare. A local connection instead uses the token
from the user's own device and sends it to Canvas.

The notice is a plain-language beta warning, not a legal opinion or a guarantee
of immunity from liability. Its limitation language does not replace reasonable
security, accurate privacy disclosures, or the operator's applicable duties.
The FTC's [security guidance](https://www.ftc.gov/business-guidance/resources/start-security-guide-business)
recommends minimizing stored data, controlling access, protecting sensitive
information, and keeping security procedures current.

## Separate Canvas policy issue

Canvas's [OAuth documentation](https://developerdocs.instructure.com/services/canvas/oauth2/file.oauth)
expressly states that applications used by multiple users must obtain Canvas
tokens through OAuth, and that asking other users to manually generate and
enter tokens violates its API policy. Built by Aadi OAuth authenticates the
assistant to this connector; it does not satisfy Canvas's upstream OAuth
requirement. The hosted PAT implementation must not be described as
institution-approved or as resolving that policy issue. Open-source licensing
and the beta notice do not change Canvas's policy.

## Display and release rules

- Keep the local setup recommendation visible without an account.
- Put the beta notice immediately before the authenticated Canvas token form.
- Explain that token creation appears only when the school enables it; never
  suggest bypassing a disabled control.
- Do not publish a working-hosted-service claim from the presence of code alone.
  The static UI requires validated hosted configuration and separately reports
  whether new account setup is available.
- Keep this copy and the server-rendered notice consistent when either changes.
