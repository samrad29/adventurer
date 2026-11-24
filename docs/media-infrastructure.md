# Media infrastructure

This document is the asset lifecycle, storage, optimization, and delivery note for The Adventurer.

It does not replace the contracts that already own a decision:

| Document | Owns |
| --- | --- |
| `docs/architecture.md` | Domain invariants, ownership, licensing, and publication rules. Section 17 is awards. Section 18 is narrative media. Section 19 is branding. |
| `docs/content-authoring.md` | The route package, validation, and import. |
| `docs/dynamic-home.md` | Scene selection and the home presentation. |
| `docs/api-v1.md` | HTTP fields and routes. |
| This document | Where bytes live, how a photograph becomes a runtime asset, how a client should fetch it, and how that delivery can change later. |

Three labels are used below.

- **Implemented.** The code and the applied schema do this today.
- **Approved, not built.** A contract already chooses the approach. The bucket, upload, or screen does not exist.
- **Proposed.** A target for discussion. It is not a schema rule, an upload limit, or a client requirement until a later decision accepts it.

## 1. Media categories

Three categories stay separate. A file in one category is not a substitute for a file in another.

### 1.1 Curated route media

Implemented registry. Not yet filled for launch routes.

| Role | What it is | Who reads it |
| --- | --- | --- |
| `route_hero` | The approach photograph, before the first checkpoint. | Home scene, and only when no reached checkpoint has a usable scene. |
| `checkpoint_scene` | The photograph of one checkpoint. | Home scene, after that checkpoint is reached. |
| `journal` | Extra photographs on a reached checkpoint's field-journal entry. | The story detail. Absent from home. |
| `discovery` | Photographs for one discovery on a reached checkpoint. | That discovery on the story detail. Absent from home. |

The version-controlled package under `content/routes/<slug>/` is the editorial source. `docs/content-authoring.md` is the format. Published `route_media` rows are the only runtime authority. A filename, a directory listing, and `adventurer/photo_credits.json` do not choose a photograph.

`content/routes/sample-ridge/` is a fictional package. Its metadata does not describe the real file it points at. It is not launch content and it is not imported at startup.

### 1.2 Organization and expedition branding media

Approved references. Upload is not built.

| Asset | Column | Scope |
| --- | --- | --- |
| Organization logo | `organizations.logo_ref` | That organization's expeditions, through the branding resolution in section 19. |
| Organization cover | `organizations.image_ref` | The same organization layer. This is card artwork, not a route scene. |
| Expedition cover override | `expeditions.image_ref` | That expedition only. It replaces the cover. There is no expedition logo and no expedition primary color. |

Inheritance, who may edit, and the resolved `branding` object are section 19. A branding edit can clear `logo_ref` or `image_ref` with JSON `null`. It cannot set a path. No reader checks that a file exists.

Future uploads use a separate object store, section 19.7. Those bytes do not go in PostgreSQL, `adventurer/static/photos`, or `route_media`. The bucket is not created. Supabase Storage is not provisioned.

### 1.3 Product and collectible media

Proposed category. No storage model.

This category is platform-managed artwork rather than route editorial or sponsor branding:

- Application artwork, including the product mark used when an organization has no logo.
- Achievement illustrations.
- Digital trophies, medals, and pins.
- Merchandise photography.

Section 17 defines awards and says a collectible is a presentation of one earned award. That section is not implemented. This document does not add a table, a path column, an upload route, or a file layout for it. Collectible bytes are not route photographs and they are not organization logos.

## 2. Curated media lifecycle

The intended editorial workflow for a new route photograph:

1. Source the original photograph outside this repository.
2. Record provenance and licensing beside that original: author, license, license URL, source URL, and, for a commission, the permission note.
3. Verify that the rights allow this commercial use. A string that matches the allowlist is not proof. Section 18.4 is the publication gate.
4. Review and approve the photograph for a named route, role, and checkpoint or discovery.
5. Make a mobile-optimized derivative. Section 4 is the proposed target, not a current processor.
6. Add only that approved derivative to the repository, under `adventurer/static/photos/`.
7. Reference the site-relative path from the route package, with alt text and the rights record.
8. Validate the package. `validate --database` also checks the route, checkpoints, spans, and position occupancy.
9. Dry-run the import, then import. The importer writes narrative rows only. It does not change journey progress.
10. The API delivers a published row whose file still resolves inside the curated directory.

Originals, contact sheets, unapproved candidates, and working files stay outside the application repository. The repository holds approved runtime derivatives.

The historical Everest JPEGs already in `adventurer/static/photos/` are an exception kept for editorial review. They are not approved launch derivatives. `adventurer/photo_credits.json` is the archival attribution for those files, keyed by stop slug. Nothing in the running application reads it. An entry there does not publish a row. A later Everest package may copy a reviewed credit from it. The file is not a runtime dependency and it is not a second attribution source.

Those historical JPEGs are about 100 KB to about 1.1 MB. They have not been through the optimization targets in section 5.

## 3. Current runtime architecture

Implemented, except where a paragraph is marked otherwise.

### 3.1 Where the bytes are

Curated photographs are files in `adventurer/static/photos/`. Flask serves that directory at `/static/photos/` through the application's static handler. The files present today are the historical Everest set: `base-camp.jpg`, `camp-1.jpg`, `camp-2.jpg`, `camp-3.jpg`, `dingboche.jpg`, `gorak-shep.jpg`, `kala-patthar.jpg`, `lobuche.jpg`, `lukla.jpg`, `namche.jpg`, `south-col.jpg`, `summit.jpg`, and `tengboche.jpg`, plus `.gitkeep`.

`route_media.asset_path` stores a site-relative path such as `/static/photos/lukla.jpg`. The table does not store bytes, width, height, or a thumbnail. Nested paths are allowed when every segment is a safe filename. The scene selector does not infer a path from a route slug or a checkpoint slug.

There is no legacy filename lookup and no fallback to `photo_credits.json`. A route with no published, readable hero or checkpoint scene returns a null scene. That is the current Everest result, because launch rows are not seeded. Section 18.7 step 3 is the remaining import.

### 3.2 What the API will return

Publication status on the row is `draft`, `published`, or `retired`.

Home scene selection reads published `route_hero` and `checkpoint_scene` rows for the journey's route, in one select on the caller's transaction. The order is `docs/dynamic-home.md`: the highest reached checkpoint with a readable scene, then the nearest lower reached checkpoint with a readable scene, then the route hero, then null. A checkpoint is reached only when every one of its thresholds is met. Journal and discovery rows are not candidates. An organization logo and an expedition cover are not candidates.

The story detail reads published `journal` and `discovery` rows for a checkpoint the caller has reached. Draft and retired rows are not selected. The journal list does not include image bodies. Anonymous route pages do not include journal or discovery images.

Public attribution on a returned photograph comes from the row: alt text, author, license, license URL, source URL, and rights basis. `permission_note` is stored for a commission and is not selected. Home always returns the five credit fields, using null when the column is null. The journal payload omits a null author, license URL, or source URL. `docs/api-v1.md` is the field contract.

A published row must already satisfy the section 18.4 rights check. The importer rejects a package that would publish an incomplete or noncommercial record. Draft and retired rows may be incomplete.

### 3.3 Path validation

Implemented in `adventurer/narrative.py` and reused by the importer.

A path is readable only when all of these hold:

- It is a string with no surrounding whitespace.
- It starts with `/static/photos/`.
- Each segment starts with a letter or digit and otherwise contains only letters, digits, `.`, `_`, and `-`, and is at most 128 characters.
- The resolved file stays inside `adventurer/static/photos/`.
- It is a regular file. A symlink that resolves outside the directory is rejected.

The importer requires that check for every status, including drafts. After import, a published row whose file is missing, malformed, or outside the directory is skipped. The API omits that image and continues the scene fallback. It does not invent another path.

A file on disk without a published row is not eligible. A static file alone is not publication.

### 3.4 Retirement and identity

Media identity is role, checkpoint, discovery, and position. `docs/content-authoring.md` is the update and omission rule. Position namespaces are enforced before write:

- One published hero per route, and one published checkpoint scene per checkpoint.
- One non-retired journal image per checkpoint and position.
- One non-retired discovery image per discovery and position.

Retired rows stay in the table and keep their ids. They drop out of those indexes. Naming the same identity again updates that row. Omitting a row leaves it, including a published row. Retirement is how a photograph leaves the API. The import does not delete the file.

### 3.5 Static files are not confidential

The narrative gate controls which paths the JSON API returns. It does not control who can fetch a file.

`GET /static/photos/<path>` does not consult `route_media`. Anyone who knows the URL can request the bytes. A request with no `Authorization` header is served. A request with an invalid bearer token is rejected by the application's auth hook before the file is read, so an image request should not send a token. A missing file uses the application's JSON 404, not an image body.

Draft and retired rows can still point at a file that remains publicly fetchable. Removing a photograph from the product requires both a registry change and a byte change. Section 7.4.

The application does not set a media cache policy. Responses do get `X-Content-Type-Options: nosniff`.

## 4. Image optimization

Proposed. No processing script, no new column, and no upload validator uses this table.

These are mobile-first starting targets. They are not schema limits. Branding uploads, when built, use the type, size, and pixel caps in section 19.7. The logo and cover rows here match that section's recommended frames and are tighter file-size targets than its hard caps.

| Asset | Recommended dimensions | Target file size |
| --- | --- | --- |
| Immersive checkpoint scene | 1440 × 1920 | 250–500 KB |
| Route hero | 1600 × 1200 | 200–400 KB |
| Field journal image | 1200 px long edge | 100–250 KB |
| Discovery image | 1000 px long edge | 80–200 KB |
| Organization logo | 512 × 512 | Under 100 KB |
| Organization cover | 1200 × 675 | 150–300 KB |

### Formats

JPEG is the current curated format and fits photographs that do not need transparency. WebP is a reasonable derivative format for the same pictures if the client can decode it, which current Expo clients can. PNG is for logos and other graphics that need a hard edge or alpha. Section 19.7 already refuses SVG and GIF for branding uploads.

Do not ship a photograph as PNG merely to avoid choosing a JPEG or WebP quality.

### Frame, crop, and focal point

Heroes are a wide landscape frame. Checkpoint scenes are a tall portrait frame so a phone can hold the place without a letterbox. Journal and discovery images keep the source aspect ratio within the long-edge cap. Logos are square. Covers are 16:9.

Crop toward the subject a person needs to recognize the place. There is no focal-point column. Until one exists, the client should center the frame and keep the aspect ratio. Stretching a photograph to the screen is not acceptable.

### Compression and metadata

Choose the lowest quality that still holds edges and text in the scene at phone size. A quality around 70–80 for JPEG or WebP is the proposed starting point, checked on a phone, not only on a desktop zoom.

Strip camera metadata, GPS, and embedded thumbnails from the derivative. Keep any copyright or attribution notice that the license requires to travel inside the file. The API attribution is still required. Stripping EXIF does not satisfy a license that requires the author to be shown beside the picture.

The processing step must follow the license. A no-derivatives license is already outside section 18.4 and must not be published. A license that allows derivatives still requires the attribution the basis records.

### Names and versions

The filename is not the selector. A descriptive name is easier to review. Replacing the bytes at an existing path does not change `asset_path`, so clients and intermediaries can keep the previous image. A visible replacement should use a new path and a package update that points the same media identity at it. There is no asset-version column.

Originals stay outside the repository, named and credited well enough that a later derivative can be made again without guessing the license.

### Alt text

Every `route_media` row requires alt text, including drafts. The text describes the picture for someone who cannot see it. It is not the caption, the checkpoint name, or the credit line.

## 5. Mobile delivery

Expectations for the Expo client. No screen is specified here, and the API is unchanged.

- Resolve `image.path` and journal `path` against the configured API origin. The value is site-relative. It is not a remote URL and it does not include the origin.
- Branding `logo_ref` and `image_ref` are a different origin once section 19.7 storage exists. Until a ref is set, the client uses the product mark or omits the cover. It does not walk the inheritance chain when a file fails to load. Section 19.
- Request the image without an `Authorization` header.
- Cache by the full URL. Prefetch the active journey's scene image. Do not prefetch journal or discovery images for checkpoints the home payload did not unlock, and do not download those images to paint home. Home does not include them.
- Show a stable placeholder or the previous scene while the file loads. A JSON 404 or any non-image response is a missing photograph. Use the same placeholder the scene uses when `image` is null.
- Keep the photograph's aspect ratio. A tall scene can be cropped by the frame. It is not stretched.
- Show the attribution the API returned with the photograph: author and license, and the license or source link when the client has room to offer it. Creative Commons use depends on that credit being available where the picture is shown. `permission_note` will not be in the payload.
- Paint organization logos and expedition covers on the expedition card. Do not use them as the route scene, and do not use a route photograph as a sponsor logo.

## 6. Storage and CDN evolution

### 6.1 Initial release

Approved for the first launch. This is the implemented host.

Curated derivatives stay in the repository and are deployed with the backend. Clients load `/static/photos/...` from the API origin. The registry stays in PostgreSQL. No CDN and no object store are required to ship the first route photographs.

### 6.2 Organization asset uploads

Approved, not built. Section 19.7.

Logos and covers go to object storage with public read, evaluated as a Supabase Storage bucket and not created. Another host can serve the same refs. The column stores a stable path:

- `/branding/organizations/<organization id>/logo/<asset id>.<ext>`
- `/branding/organizations/<organization id>/cover/<asset id>.<ext>`
- `/branding/expeditions/<expedition id>/cover/<asset id>.<ext>`

A replacement uses a new asset id, then updates the column. The client keys its cache on that path. The read API does not check the bucket. This store is not the route-photo directory.

### 6.3 Curated route media at a larger scale

Proposed. Decide later.

Move route derivatives to object storage and a CDN when the repository size, deploy time, origin bandwidth, or image latency justifies it. The trigger is measured cost or delay, not a preference for a vendor.

A later migration can keep the editorial identity stable:

- The package and `route_media` remain the authority for role, checkpoint, discovery, position, status, alt text, and rights.
- `asset_path` changes from `/static/photos/...` to the new public URL or site-relative key.
- Row ids stay, so a retirement still hides the same photograph.
- Bytes move. The table still does not store them.
- Journey progress, scene fallback, and journal gating stay as they are.

That move is not approved as current work. Supabase Storage is not provisioned for it.

### 6.4 Unresolved delivery questions

These need a decision before a route-photo migration, and some of them matter earlier:

- Which `Cache-Control` the first static files should send, and how a same-path replacement becomes visible.
- Whether `asset_path` remains a path on the API origin or becomes an absolute CDN URL. The current client contract is a site-relative path.
- How long a replaced object must stay readable for clients that cached the old path.
- Who may read unpublished objects if drafts ever leave the repository. Today's drafts are ordinary static files.
- How a deploy of bytes and a commit of registry rows are ordered. See section 7.
- Which host is chosen. Section 19.7 names a Supabase bucket as the branding candidate. That choice is not a provisioned route-photo bucket.

## 7. Operations

### 7.1 Import and deploy

Implemented constraint. The importer reads `TEST_DATABASE_URL` and refuses every other target, including hosted Supabase and Render. Validating a package does not publish it to production.

The path check looks at the files on the machine running the import. It does not look at the deployed host. A row can be published in a database whose running server does not yet have the file. The API then omits the image until the file is deployed.

For launch, the derivative has to be in the deployed `adventurer/static/photos/` tree before or at the same time as the registry row becomes visible. The production import step itself is not built.

### 7.2 Missing files and orphan references

A package that names a missing file is rejected, so an import cannot create that row. A file deleted after a successful import leaves the published row in place. Reads skip it. The next package that omits the row also leaves it in place. Retire the row in a package to drop it from the API.

A file with no row is unused. It is still publicly fetchable. Removing it is a repository change, not an import.

### 7.3 Replacement and cache

Same path, new bytes: caches may keep the old picture until their own lifetime ends. The application does not append a version query. New path, package update: the identity stays and clients request the new URL after the next API read.

Branding replacements, once uploads exist, follow the new asset id in section 19.7. Deleting the previous object is allowed only after the column update commits. A failed delete leaves an unreferenced object. The column remains the source of truth.

### 7.4 Retirement is not removal of the bytes

Retiring a `route_media` row hides it from home and from the journal. The file remains on disk and at its URL. Deleting or replacing the file removes public access to those bytes and does not, by itself, retire the row. A takedown does both: retire or replace the registry row, and remove the deployed bytes. Do not treat a JSON omission as a copyright takedown.

`permission_note` and `photo_credits.json` are editorial records. They are not the public credit and they are not a kill switch.

### 7.5 Failed import

A failed import rolls the editorial transaction back. Journey rows, credits, and acknowledgments are not part of that write. Files already committed to git are not rolled back by the importer. If a deploy contained a file the database did not accept, the file is an unused public object until someone removes it.

### 7.6 Monitoring

Proposed. Nothing in the application records image bandwidth.

Watch the size of `adventurer/static/photos/` in the repository, the time to deploy that tree, origin bytes served for `/static/photos/`, and the rate of non-image responses for those paths. Those are the signals in section 6.3.

## 8. Outstanding decisions before mobile development

Mobile work can use the implemented scene and journal paths now. These decisions are still open and should be settled before treating the targets above as build rules:

1. Accept, tighten, or replace the section 4 dimension and file-size targets.
2. Choose JPEG, WebP, or both for the first curated derivatives.
3. Keep center cropping until a focal point exists, or add a focal point later. This document does not add the column.
4. Decide where CC attribution sits on the scene and on the journal image. The fields are already in the API.
5. Choose `Cache-Control` for `/static/photos/` before the first production deploy of optimized files.
6. Define how a validated package is applied to the hosted registry. The importer will not write that database.
7. Import reviewed Everest rows only after section 18.4 accepts each photograph. Section 18.7 step 3. The historical files and `photo_credits.json` are the editorial input, not the published registry.
8. Keep branding refs empty until the section 19.7 bucket exists. The client needs a product placeholder either way.
9. Leave collectible artwork unspecified until section 17 is implemented. Do not store it in `route_media`.
