# Domain packs

A domain pack is one YAML file that installs a reporting solution into DIS:
raw-SQL queries, the datasets built on them, and dashboards with their widgets.
It lets a deployment be rebuilt from source control instead of by hand in the UI.

```
packs/
  README.md           this file
  example/            a tiny runnable pack (used by the tests)
    pack.yaml
    orders.sql        its SQLite demo data
  <your-pack>/
    pack.yaml
```

## Installing a pack

```bash
python manage.py seed_domain_pack --pack packs/<name>/pack.yaml \
    [--owner USERNAME] [--recipients "a@x.com,b@y.com"] [--refresh] [--dry-run]
```

| Option | Meaning |
|---|---|
| `--pack` | Path to the YAML file (absolute, or relative to the working directory or the backend root). |
| `--owner` | User who owns the created queries, datasets and dashboards. Default: the first active superuser (then the first active `admin`). |
| `--recipients` | Comma-separated addresses for every scheduled report in the pack; validated, de-duplicated, and the reports are turned on (an empty value turns them off). Omit it to keep the recipients already set. |
| `--dry-run` | Validate, apply inside a transaction, print what would change, roll back. Nothing is saved or refreshed. |
| `--refresh` | After applying, refresh the pack's datasets and print each one's row count (or error). Exits with status 1 when any refresh fails; the pack itself stays applied. |

Behaviour:

- **Validate first, all at once.** Structure, keys, read-only SQL (the same
  `assert_read_only` guard the API uses), declared parameters, data sources,
  groups and name clashes are all checked before anything is written. Every
  problem is reported in one error; if there is any, nothing changes.
- **Create or update by name**, in one transaction. Each pack dataset owns one
  raw-SQL query *and* one dataset, both named `name`. Each dashboard is matched by
  `name`; its widgets are replaced when they differ from the pack (so a re-run
  restores anything edited in the UI) and left alone when identical (widget ids
  stay stable). Each report is matched by `name`; the pack owns its dataset,
  schedule and format, the Reports page owns its recipients (see `reports`).
- **Idempotent.** Re-running an unchanged pack reports every object as
  `unchanged` and writes nothing.
- **Never touches objects not named in the pack.** It refuses (validation
  error) when a pack name would be ambiguous (two existing objects share it) or
  when the existing query of that name also feeds a dataset outside the pack.
  Objects dropped from a pack are not deleted; remove them in the UI.
- A dataset whose query or refresh interval changed gets `next_refresh_at`
  cleared, so the scheduler refreshes it on its next tick (manual datasets
  stay manual).

## Schema (version 1)

```yaml
pack: fmms                       # required: the pack's name
version: 1                       # required: schema version (only 1 exists)
datasources:                     # required: key -> name of an EXISTING DataSource
  default: "PMSv2 Reporting"
  restricted: "PMSv2 Reporting - Restricted"
visibility: org                  # optional: private | org (default private)
share_with_group: ""             # optional: Django group; see "Sharing"

datasets:                        # optional list
  - key: eng_progress            # required, unique in the pack: letters, digits, _ . -
    name: "ENG-01 Engineering progress"   # required, unique in the pack, <= 200 chars
    description: "..."           # optional
    datasource: default          # required: a key of `datasources`
    sql: |                       # required: ONE read-only SELECT / WITH statement
      SELECT ...
    parameters: []               # optional: QueryDefinition.parameters
    refresh: daily               # optional: manual | hourly | daily | weekly (default manual)
    share_with_group: ""         # optional extension: overrides the pack-level group

dashboards:                      # optional list
  - key: executive               # required, unique in the pack
    name: "PMSv2 · Executive"    # required, unique in the pack, <= 200 chars
    description: "..."           # optional
    filters: []                  # optional: Dashboard.filters JSON, stored as-is
    share_with_group: ""         # optional extension: overrides the pack-level group
    position: 10                 # optional: where the board is listed (default 10, 20, ... in pack order)
    widgets:                     # optional list, in display order
      - {title: "...", type: kpi, dataset: eng_progress,
         config: {value_field: x, rollup: avg}, layout: {x: 0, y: 0, w: 3, h: 4}}

reports:                         # optional list: scheduled, audited dataset exports
  - key: weekly_progress         # required, unique in the pack
    name: "PMSv2 weekly · Progress"  # required, unique in the pack, <= 200 chars
    dataset: eng_progress        # required: a dataset key of this pack
    schedule: weekly             # required: daily | weekly | monthly
    format: pdf                  # optional: xlsx | pdf | csv (default xlsx)
```

Field rules:

| Field | Rule |
|---|---|
| unknown keys | Rejected at every level (catches typos such as `widgts`). |
| `datasources.*` | Must match exactly one existing DataSource by name. The pack never creates data sources (they hold credentials). |
| `visibility` | Set on every query, dataset and dashboard of the pack, and enforced by `core.access`: `private` = the owner plus the members of `share_with_group`; `org` = every signed-in user of the deployment. `lab` is refused: a pack sets no laboratory, and LAB without one grants nobody. Admins see everything. |
| `sql` | Must pass `assert_read_only` (one `SELECT`/`WITH`, no DML/DDL, no `SELECT ... INTO`). Write it in the data source's SQL dialect. Bind parameters use `:name`. |
| `parameters` | List of mappings with at least `name` (a bind name); `type`, `default`, `required` are stored as given (QueryDefinition semantics: a non-required parameter's `default` fills the bind when no value is supplied). Every `:name` used in `sql` must be declared here. |
| `refresh` | A `Dataset.refresh_interval` value. |
| widget `type` | One of `bar, line, pie, area, scatter, histogram, heatmap, boxplot, kpi, table, text, studio`, and the management views `bar_list` (ranked top-N: `category_field`, `value_field`, `limit`, `unit`), `funnel` (pipeline stages in row order: `category_field`, `value_field`; shows conversion from the first stage) and `progress` (value against a goal: `value_field` with `target` or `target_field`, optional `category_field` for one bar per row, `direction`). Any widget may set `caption`: one plain sentence saying what the number means for the business. |
| dashboard `position` | Boards are listed by `position`, smallest first, then boards without one, newest first. A pack's boards default to 10, 20, 30, ... in the order written, so the first board in the file is the one a manager sees first; set it to slot a board between two others (the restricted Commercial Values board uses 35 to follow Commercial & Bids at 30). |
| widget `dataset` | A dataset `key` of this pack; required except for `text` and `studio` widgets. |
| widget `config` | Mapping, stored as-is (e.g. KPI `{value_field, rollup}`, charts `{category_field, value_field}`, table `{}`). Management options, all optional: **KPI** `label` (what the number is, in words), `unit`, `direction` (`higher_better` / `lower_better`), `trend_field` + `value_from` (`last` = the latest period; `last_complete` = the latest finished period, so a month-to-date count is never compared with a full month), `delta_label`, `thresholds` (`[{op, value, color, label}]`), and the info icon shows the dataset's `caveat` column (or `note_field`). **Any chart or list** `tones`: `{category or series name: good / watch / serious / act / info / neutral}` for categories that carry meaning (age bands, statuses); everything else uses the colour-blind-safe palette. **bar_list** `detail_field` + `detail_format` (a second figure in words, e.g. `"median {value} days waiting"`), `sort: false` (keep the dataset's order). **bar / line** `category_format: month` ("Jun 2026"), `reference: {value, label}` (an agreed target as a dashed line), `orientation: horizontal`, `stacked`. **table** `columns: [{field, label, format}]` (pick, order and name the columns; others are hidden; numbers get thousands separators except identifiers, years and codes, or any column with `format: raw`), `page_size`, `row_rules`. **Units**: `%`, `h`, `d`, `ratio`, or a currency code, which reads first (`SAR 1.2M`). Lists show as many rows as fit their card (tightening their rows first) and say how many more; `limit` is the maximum. **bar_list / progress** `name_field`: a second name beside each category, e.g. `2419 · <project name>` (the category stays the identity a click filters on). **The list behind a figure** (KPI, bar_list, progress): `drill: {dataset, field, filters, columns, title, label}`. `dataset` is a dataset key of this pack (stored with its id on install); clicking a bar opens that list filtered by `field` = the bar's category, a KPI opens the whole list through a link reading `label` ("See the documents"); `filters: [{column, value}]` narrow every opening; `columns` as on a table; `title` names the window. The list is paged in the database and can be downloaded (audited). **Make the list count exactly what the figure counts** (same filters, same labels) and end its `ORDER BY` on output columns with a unique key: the database keeps a trailing `ORDER BY` only when it names output columns, and pages of a non-unique order can repeat or skip rows. A manager can click only figures that have a list. |
| widget `layout` | Integers `x, y >= 0`, `w, h >= 1` on a 12-column grid; missing ones default to `x=0, y=0, w=4, h=6`. When every widget of a board sits inside the 12 columns and none overlaps, the board is laid out by position (cards can stack beside a tall chart); otherwise widgets flow in order. A text widget with `h: 2` renders as a section heading. |
| `reports` | Sent by `send_scheduled_reports`: one email and one audit row per recipient, the file rendered by the branded exporter. **Recipients are never in a pack** (personal data that changes without a release; a `recipients` key is rejected): `seed_domain_pack --recipients "a@x.com,b@y.com"` sets them on every pack report and turns them on (an empty value turns them off). Without `--recipients`, a new report is created inactive and an existing one keeps the recipients and on/off state set on the Reports page. |
| report `board` | Instead of `dataset`: the whole board, as a PDF drawn on the server (`core.board_render`: sections, KPI cards with their change, lists, progress, the first rows of tables, bar and line charts; a figure that cannot be computed says so). Only from a pack with `visibility: org`: a private board (bid values, costs) is shared by link only, and the send re-checks it. `format` is `pdf`. Set `DIS_APP_URL` to put the board's address in the email. The same PDF downloads from the board page ("Download PDF" for anyone who can open the board, audited). |
| YAML values | Dates written bare (`2026-01-01`) are stored as ISO text. Mapping keys must be text: quote `on`, `off`, `yes`, `no` (YAML reads them as booleans). |

### Data-as-of convention

Exports and scheduled reports state how old the **source** data is, not only when
DIS last refreshed its cache. The source time is taken from, in order:

1. a column named `data_as_of` (any case) in the dataset's result: its latest value;
2. the data source's `options["freshness_sql"]`: one read-only `SELECT` returning one
   value (for the PMSv2 reporting copy, `SELECT data_as_of_riyadh FROM rpt.v_data_as_of`).
   It runs with the data source's own read-only login; if it fails, the export still
   goes out with the refresh time.

With a source time, the file shows **Data as of** (the source time) and
**Refreshed** (DIS's refresh). Without one, **Data as of** is the refresh time.

### Sharing

`share_with_group` names an existing Django group. When set, the pack's queries,
datasets and dashboards are shared with exactly that group's members at seed time
(the pack is the source of truth: members who left the group lose access on the
next run). Membership changes made later take effect when the pack is re-run.
Leave it empty to keep whatever sharing exists.

A dataset or dashboard entry may set its own `share_with_group`, overriding the
pack-level one. This is an extension of the base schema, meant for restricted
content (for example cost data shared with a smaller group than the rest).

## Trying the example

```bash
sqlite3 /tmp/example_orders.sqlite < packs/example/orders.sql
# register a SQLite data source named "Example Source" whose database path is
# /tmp/example_orders.sqlite (UI: Connections -> New), then:
python manage.py seed_domain_pack --pack packs/example/pack.yaml --dry-run
python manage.py seed_domain_pack --pack packs/example/pack.yaml --refresh
```

## Related operations

| Command / endpoint | Use |
|---|---|
| `manage.py refresh_datasets --datasource NAME [--datasource ...]` or `--all` | Refresh datasets synchronously; prints per-dataset rows and timings; exit status 1 on any error. |
| `manage.py promote_source --datasource NAME [...] --database DB [--restore-id ID] [--refresh]` | Point data source(s) at a newly restored database (A/B promote), audited; optionally refresh the datasets that read them. |
| `GET /api/datasets/<id>/export/?format=xlsx\|pdf\|csv[&params=<json>][&filters=<json>]` | Audited, branded export of a dataset's full result. |
| `manage.py send_scheduled_reports` | Sends due `DatasetReport`s in their `export_format`; one audited message per recipient. |
