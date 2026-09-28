# CertificateFlow production fixes

Implemented in this build:

- Persistent portal state is reloaded from PostgreSQL before each certificate-portal API request so Vercel warm workers do not keep stale participant/template/certificate/email data.
- Certificate template page dimensions are stored and automatically backfilled for older templates.
- Template preview uses the actual stored page aspect ratio instead of assuming A4 for every template.
- PDF templates are visible behind the interactive field layer in the template editor.
- Template X/Y coordinates use a consistent center-anchor model in both the editor and generated PDF.
- Generated PDF text converts the saved X/Y percentage to the actual template page dimensions.
- Generated PDF vertical placement uses the selected font's ascent/descent so the visible text center matches the editor's Y coordinate.
- Font family, size, weight, italic/bold-italic style, color, and alignment are applied by the server renderer.
- Certificate fonts are bundled so Vercel does not depend on machine-installed fonts.
- Certificate downloads now retrieve the server-generated PDF instead of generating a separate hard-coded client-side certificate.
- Email delivery now reports clear Brevo connection/API/configuration errors.
- Certificate approval remains the one-click workflow: approve -> generate -> email.
- Dashboard stale demo eligibility counts and admin name were removed.
- Existing template dimension metadata is migrated automatically when templates are read.
