# Render Free + Supabase Free + Vercel

Render runs Python/FastAPI and temporary processing files. Supabase stores
courses, publication choices, document sections and metadata in PostgreSQL,
and original PDFs plus PDF/Markdown/HTML/JSON outputs in a **private** Storage
bucket. Vercel proxies the public website URL to Render.

## Create Supabase resources

1. Create a Supabase **Free** project, preferably near Render's Frankfurt region.
   Keep the database password for the connection string.
2. In **Storage**, create a bucket named `ragademi` with **Public bucket OFF**.
   Sources and document state must stay private. Set the global and bucket
   upload limits to 50 MB. The bucket stores PDF, HTML, Markdown and JSON;
   avoid a PDF-only MIME restriction.
3. Under **Connect**, select **Session pooler** and copy its PostgreSQL URL
   (port 5432). Replace the password placeholder with the URL-encoded password.
   Use the dashboard's actual hostname, not a guessed region hostname.
4. In Storage's **S3 configuration**, enable S3, generate an access key pair,
   and copy its endpoint and region. Use these server-side S3 keys, not the
   public Supabase `anon`/publishable key.
5. Put the settings below into Render's environment variables. Keep the
   database URL and S3 keys out of Git, browser code and Vercel. The app creates
   its database tables automatically inside the `ragademi` schema. The default
   Supabase Data API's `public` schema is not used for the library.

| Render variable | Value |
| --- | --- |
| `DERSNOTU_LIBRARY_BACKEND` | `postgres` |
| `DERSNOTU_DATABASE_URL` | Supabase Session pooler PostgreSQL URL |
| `DERSNOTU_S3_ENDPOINT` | Endpoint copied from Supabase S3 configuration |
| `DERSNOTU_S3_REGION` | Region copied from Supabase S3 configuration |
| `DERSNOTU_S3_ACCESS_KEY` | Generated S3 Access Key ID |
| `DERSNOTU_S3_SECRET_KEY` | Generated S3 Secret Access Key |
| `DERSNOTU_S3_BUCKET` | `ragademi` |
| `DERSNOTU_ADMIN_PASSWORD` | Long unique owner password |
| `ANTHROPIC_API_KEY` | Optional; leave blank for demo output |

The `render.yaml` Blueprint already sets `plan: free`, the local working
directories, a 50 MB upload limit, a 100 MB batch limit, and one generation
worker. It attaches **no Render disk**. Cloud configuration is mandatory;
missing settings stop startup rather than silently using a temporary library.

If startup reports `CERTIFICATE_VERIFY_FAILED` or a self-signed certificate
in the database certificate chain, download the root certificate from Supabase
**Database Settings -> SSL Configuration -> Download Certificate**. In the
Render service's **Environment -> Secret Files**, add a file named
`supabase-ca.crt` containing the complete PEM certificate (including the
BEGIN/END CERTIFICATE lines). Add the environment variable
`DERSNOTU_DATABASE_SSL_CA_FILE=/etc/secrets/supabase-ca.crt` and redeploy.
The app adds this CA to its trust store and continues verifying both the
certificate chain and server hostname. Do not disable certificate verification.

## Deploy

1. Commit and push the project to GitHub.
2. Render: **New → Blueprint**, select the repository and `render.yaml`, enter
   the environment variables above, and deploy. Do not select a paid instance
   or add a persistent disk. Wait for the service to become healthy.
3. Open Render's `https://...onrender.com/public` and its owner interface `/`.
   Sign in as `admin` with the configured password.
4. Vercel: import the same repository, Framework Preset **Other**. The
   `vercel.json` file routes the interface and API to
   `https://ragademi.onrender.com`. No Vercel environment variables are needed;
   update the destination in this file if the Render URL changes.
5. Test a small course upload and publish a generated PDF. Redeploy Render and
   confirm the course and public PDF still load. Your cloud credentials are
   necessary for this final check; local automated tests use simulated storage.

Public PDFs are served through an app endpoint that checks `is_public` on each
request. Making a document private blocks new requests immediately. The bucket
remains private, even for published documents; no permanent public or signed
Storage URL is given to visitors. Already downloaded copies cannot be revoked.

## Copy an existing local library

New cloud installs start with an empty library; local files are not copied just
by setting environment variables. Keep your local app stopped during import.
Install the updated dependencies and add the cloud settings to your local
`.env` while leaving `DERSNOTU_LIBRARY_BACKEND=sqlite` until the copy succeeds.

From the repository directory in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
# Local preflight: reads existing data and checks file sizes, without cloud writes.
.\.venv\Scripts\python.exe scripts\migrate_library_to_cloud.py
# Explicitly copy to an empty cloud library; preserve record IDs and publication.
.\.venv\Scripts\python.exe scripts\migrate_library_to_cloud.py --apply
```

Back up `.cache/library.sqlite`, `.cache/materials` and `out` before import.
The source is opened read-only and is never deleted. The target must be empty;
all metadata is inserted in one transaction after file uploads complete. If an
upload or connection fails, the copy stops; uploaded orphan objects may need
manual removal from Storage before trying again. Do not blindly retry after an
uncertain database commit: inspect the target first. Retrieval indexes are
rebuilt locally from the stored books instead of being imported.

## Free plan limits

- Supabase Free includes **500 MB database**, **1 GB file storage**, **5 GB
  uncached egress**, and a **50 MB per-object** upload limit. Total source and
  output files both count toward Storage. Free projects can pause after a week
  of low activity; restore them from the Supabase dashboard when needed.
- Render Free has **512 MB RAM** and sleeps after 15 minutes without incoming
  traffic. First access can be slow. One generation worker reduces parallel
  memory use, but a large book or image-heavy PDF can still exceed memory.
- Render restarts discard local caches and **unfinished jobs**, while completed
  cloud records and files remain. Book indexes are rebuilt as needed, which
  adds processing time after a restart.
- Vercel's external proxy has a **120-second** request timeout. The course page
  reconnects progress streams and polls the active job's state. If the platform
  interrupts a long request, use the Render URL directly.
- These free quotas cover hosting and storage. Real Anthropic API generation
  has separate token charges; the demo backend makes no paid model calls.

Official references: [Supabase pricing](https://supabase.com/pricing),
[file limits](https://supabase.com/docs/guides/storage/uploads/file-limits),
[PostgreSQL connections](https://supabase.com/docs/guides/database/connecting-to-postgres),
[S3 credentials](https://supabase.com/docs/guides/storage/s3/authentication),
[Render Free](https://render.com/docs/free),
[Vercel proxy limits](https://vercel.com/docs/limits).
