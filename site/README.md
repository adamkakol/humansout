# humansout.si

Static site (English at `/`, Polish at `/pl/`), served by the Cloudflare Worker `humansout`,
custom domain `humansout.si`. `public/_headers` sets a strict Content-Security-Policy: no inline
scripts or styles in the pages.

```bash
cd site
npx wrangler versions upload      # preview URL, not live
npx wrangler versions deploy      # make a version live
npx wrangler rollback             # back to the previous version
```

Copy must only claim what the released SDK does; planned features carry the "Planned" tag.
