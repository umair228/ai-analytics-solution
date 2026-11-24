"""Domain packs: one YAML file that installs a reporting solution —
raw-SQL queries → datasets → dashboards → widgets → scheduled reports — idempotently.

The schema is documented in ``packs/README.md``. :func:`load_pack` parses and
validates everything up front (structure, read-only SQL, declared parameters,
data sources, groups, name clashes), reporting every problem at once;
:func:`apply_pack` then creates or updates the pack's objects by name. Objects
not named in the pack are never modified.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from django.contrib.auth.models import Group

from connections.models import DataSource
from dashboards.models import Dashboard, Widget
from datasets.models import Dataset, DatasetReport
from querybuilder.executor import QueryError, assert_read_only, sql_bind_params
from querybuilder.models import QueryDefinition

SUPPORTED_VERSIONS = (1,)
PACK_KEYS = ("pack", "version", "datasources", "visibility", "share_with_group",
             "datasets", "dashboards", "reports")
DATASET_KEYS = ("key", "name", "description", "datasource", "sql", "parameters",
                "refresh", "share_with_group")
DASHBOARD_KEYS = ("key", "name", "description", "filters", "widgets", "share_with_group")
WIDGET_KEYS = ("title", "type", "dataset", "config", "layout")
REPORT_KEYS = ("key", "name", "dataset", "schedule", "format")
REPORT_FORMATS = ("xlsx", "pdf", "csv")  # what send_scheduled_reports renders for a dataset
# Recipients are personal data and change without a release, so a pack never carries them:
# seed_domain_pack --recipients (or the Reports page) sets them.
RECIPIENT_KEYS = ("recipients", "recipient_emails")
LAYOUT_DEFAULTS = {"x": 0, "y": 0, "w": 4, "h": 6}  # the Widget model's defaults
DATASETLESS_WIDGETS = (Widget.Kind.TEXT, Widget.Kind.STUDIO)
NAME_MAX = 200
_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


class PackError(Exception):
    """The pack is invalid; ``errors`` lists every problem found."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


@dataclass
class PackDataset:
    key: str
    name: str
    description: str
    datasource: DataSource
    sql: str
    parameters: list
    refresh: str
    share_group: Group | None


@dataclass
class PackWidget:
    title: str
    type: str
    dataset_key: str | None
    config: dict
    layout: dict


@dataclass
class PackDashboard:
    key: str
    name: str
    description: str
    filters: list
    widgets: list
    share_group: Group | None


@dataclass
class PackReport:
    key: str
    name: str
    dataset_key: str
    schedule: str
    export_format: str


@dataclass
class Pack:
    name: str
    version: int
    source: str
    visibility: str
    datasets: list
    dashboards: list
    reports: list = field(default_factory=list)


@dataclass
class ApplyResult:
    actions: list = field(default_factory=list)   # (kind, name, action, note)
    datasets: dict = field(default_factory=dict)  # pack key -> Dataset

    def count(self, kind, action):
        return sum(1 for k, _, a, _ in self.actions if k == kind and a == action)


# ──────────────────────────────────────────────────────────────────────────
#  Loading and validation
# ──────────────────────────────────────────────────────────────────────────

def load_pack(path) -> Pack:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PackError([f"cannot read {path}: {exc}"]) from exc
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PackError([f"{path}: invalid YAML: {exc}"]) from exc
    return validate_pack(doc, source=str(path))


def validate_pack(doc, source="<pack>") -> Pack:
    errors: list[str] = []

    def err(where, message):
        errors.append(f"{where}: {message}")

    if not isinstance(doc, dict):
        raise PackError([f"{source}: a pack must be a YAML mapping, "
                         f"not {type(doc).__name__}."])
    _unknown_keys(doc, PACK_KEYS, "pack file", err)

    name = doc.get("pack")
    if not isinstance(name, str) or not name.strip():
        err("pack", "required: the pack's name.")
    version = doc.get("version")
    if version is None:
        err("version", "required (supported: 1).")
    elif isinstance(version, bool) or version not in SUPPORTED_VERSIONS:
        err("version", f"unsupported version {version!r} (supported: 1).")

    # -- data sources: key -> an existing DataSource, by exact name ----------
    source_keys: set = set()
    sources: dict = {}
    raw_sources = doc.get("datasources")
    if not isinstance(raw_sources, dict) or not raw_sources:
        err("datasources", "required: a mapping of key -> existing data source name.")
        raw_sources = {}
    for key, ds_name in raw_sources.items():
        where = f"datasources.{key}"
        if not isinstance(key, str) or not _KEY_RE.match(key):
            err(where, "keys must be identifiers (letters, digits, _ . -).")
            continue
        source_keys.add(key)
        if not isinstance(ds_name, str) or not ds_name.strip():
            err(where, "must be the name of an existing data source.")
            continue
        matches = list(DataSource.objects.filter(name=ds_name).order_by("id"))
        if not matches:
            err(where, f"no data source named {ds_name!r} exists (create it first).")
        elif len(matches) > 1:
            err(where, f"data source name {ds_name!r} is ambiguous "
                       f"({len(matches)} data sources have it).")
        else:
            sources[key] = matches[0]

    allowed_visibility = tuple(Dataset.Visibility.values)  # same on queries and dashboards
    visibility = doc.get("visibility", Dataset.Visibility.PRIVATE)
    if not isinstance(visibility, str) or visibility.strip().lower() not in allowed_visibility:
        err("visibility", f"must be one of {', '.join(allowed_visibility)} "
                          f"(got {visibility!r}).")
        visibility = Dataset.Visibility.PRIVATE
    visibility = visibility.strip().lower()

    pack_group = _group(doc.get("share_with_group"), "share_with_group", err)

    # -- datasets ----------------------------------------------------------
    datasets: list[PackDataset] = []
    dataset_keys: set = set()
    names: set = set()
    for i, item in enumerate(_list(doc, "datasets", err)):
        where = _where("datasets", i, item)
        if not isinstance(item, dict):
            err(where, "must be a mapping.")
            continue
        _unknown_keys(item, DATASET_KEYS, where, err)
        key = _key(item, where, dataset_keys, err)
        dname = _name(item, where, names, err)
        description = _text(item, "description", where, err)
        src_key = item.get("datasource")
        if src_key not in source_keys:
            err(where, f"datasource: {src_key!r} is not a key of 'datasources' "
                       f"({', '.join(sorted(source_keys)) or 'none defined'}).")
        sql = item.get("sql")
        sql_ok = isinstance(sql, str) and bool(sql.strip())
        if not sql_ok:
            err(where, "sql: required.")
        else:
            sql = sql.strip()
            try:
                assert_read_only(sql)
            except QueryError as exc:
                err(where, f"sql: {exc}")
                sql_ok = False
        parameters = _parameters(item.get("parameters"), f"{where}.parameters", err)
        if sql_ok:
            declared = {p["name"] for p in parameters}
            missing = [p for p in sql_bind_params(sql) if p not in declared]
            if missing:
                err(where, "sql uses " + ", ".join(f":{p}" for p in missing)
                    + " but 'parameters' does not declare "
                    + ("it." if len(missing) == 1 else "them."))
        refresh = item.get("refresh", Dataset.RefreshInterval.MANUAL)
        if refresh not in Dataset.RefreshInterval.values:
            err(where, f"refresh: must be one of "
                       f"{', '.join(Dataset.RefreshInterval.values)} (got {refresh!r}).")
        group = (_group(item.get("share_with_group"), f"{where}.share_with_group", err)
                 if "share_with_group" in item else pack_group)
        if dname:
            _check_name_clashes(dname, where, err)
        if key and dname and sql_ok and src_key in sources:
            datasets.append(PackDataset(
                key=key, name=dname, description=description,
                datasource=sources[src_key], sql=sql, parameters=parameters,
                refresh=refresh, share_group=group))

    # -- dashboards --------------------------------------------------------
    dashboards: list[PackDashboard] = []
    dashboard_keys: set = set()
    dashboard_names: set = set()
    for i, item in enumerate(_list(doc, "dashboards", err)):
        where = _where("dashboards", i, item)
        if not isinstance(item, dict):
            err(where, "must be a mapping.")
            continue
        _unknown_keys(item, DASHBOARD_KEYS, where, err)
        key = _key(item, where, dashboard_keys, err)
        dname = _name(item, where, dashboard_names, err)
        description = _text(item, "description", where, err)
        filters = item.get("filters") or []
        if not isinstance(filters, list):
            err(where, "filters: must be a list (Dashboard.filters JSON).")
            filters = []
        filters = _jsonable(filters, f"{where}.filters", err)
        widgets = []
        raw_widgets = item.get("widgets") or []
        if not isinstance(raw_widgets, list):
            err(where, "widgets: must be a list.")
            raw_widgets = []
        for j, w in enumerate(raw_widgets):
            widget = _widget(w, f"{where}.widgets[{j}]", dataset_keys, err)
            if widget is not None:
                widgets.append(widget)
        group = (_group(item.get("share_with_group"), f"{where}.share_with_group", err)
                 if "share_with_group" in item else pack_group)
        if dname and Dashboard.objects.filter(name=dname).count() > 1:
            err(where, f"name: {Dashboard.objects.filter(name=dname).count()} existing "
                       f"dashboards are named {dname!r}; rename or delete the duplicates.")
        if key and dname:
            dashboards.append(PackDashboard(
                key=key, name=dname, description=description, filters=filters,
                widgets=widgets, share_group=group))

    # -- scheduled reports: audited exports of a pack dataset ---------------
    reports: list[PackReport] = []
    report_keys: set = set()
    report_names: set = set()
    for i, item in enumerate(_list(doc, "reports", err)):
        where = _where("reports", i, item)
        if not isinstance(item, dict):
            err(where, "must be a mapping.")
            continue
        for k in RECIPIENT_KEYS:
            if k in item:
                err(where, f"{k}: recipients are not part of a pack (personal data that "
                           "changes without a release); pass seed_domain_pack --recipients "
                           "or set them on the Reports page.")
        _unknown_keys({k: v for k, v in item.items() if k not in RECIPIENT_KEYS},
                      REPORT_KEYS, where, err)
        key = _key(item, where, report_keys, err)
        rname = _name(item, where, report_names, err)
        ds_key = item.get("dataset")
        if ds_key not in dataset_keys:
            err(where, f"dataset: {ds_key!r} is not a dataset key of this pack.")
        schedule = item.get("schedule")
        if schedule not in DatasetReport.Schedule.values:
            err(where, f"schedule: must be one of "
                       f"{', '.join(DatasetReport.Schedule.values)} (got {schedule!r}).")
        fmt = item.get("format", "xlsx")
        if fmt not in REPORT_FORMATS:
            err(where, f"format: must be one of {', '.join(REPORT_FORMATS)} (got {fmt!r}).")
        same_name = DatasetReport.objects.filter(name=rname).count() if rname else 0
        if same_name > 1:
            err(where, f"name: {same_name} existing reports are named {rname!r}; "
                       "rename or delete the duplicates.")
        if key and rname and ds_key in dataset_keys and same_name <= 1 \
                and schedule in DatasetReport.Schedule.values and fmt in REPORT_FORMATS:
            reports.append(PackReport(key=key, name=rname, dataset_key=ds_key,
                                      schedule=schedule, export_format=fmt))

    if errors:
        raise PackError(errors)
    return Pack(name=name.strip(), version=version, source=source,
                visibility=visibility, datasets=datasets, dashboards=dashboards,
                reports=reports)


def _where(section, index, item):
    key = item.get("key") if isinstance(item, dict) else None
    return f"{section}[{index}]" + (f" ({key})" if isinstance(key, str) and key else "")


def _list(doc, section, err):
    value = doc.get(section)
    if value is None:
        return []
    if not isinstance(value, list):
        err(section, "must be a list.")
        return []
    return value


def _unknown_keys(mapping, allowed, where, err):
    for k in mapping:
        if k not in allowed:
            err(where, f"unknown key {k!r} (allowed: {', '.join(allowed)}).")


def _key(item, where, seen, err):
    key = item.get("key")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        err(where, "key: required, an identifier (letters, digits, _ . -).")
        return None
    if key in seen:
        err(where, f"key: {key!r} is used twice.")
        return None
    seen.add(key)
    return key


def _name(item, where, seen, err):
    name = item.get("name")
    if not isinstance(name, str) or not name.strip():
        err(where, "name: required.")
        return None
    if len(name) > NAME_MAX:
        err(where, f"name: longer than {NAME_MAX} characters.")
        return None
    if name in seen:
        err(where, f"name: {name!r} is used twice in this pack.")
        return None
    seen.add(name)
    return name


def _text(item, field_name, where, err):
    value = item.get(field_name)
    if value is None:
        return ""
    if not isinstance(value, str):
        err(where, f"{field_name}: must be text.")
        return ""
    return value.strip()


def _group(value, where, err):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        err(where, "must be a group name.")
        return None
    group = Group.objects.filter(name=value).first()
    if group is None:
        err(where, f"no group named {value!r} exists.")
    return group


def _parameters(value, where, err):
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        err(where, "must be a list of {name, type, default, required}.")
        return []
    out, seen = [], set()
    for i, p in enumerate(value):
        pw = f"{where}[{i}]"
        if not isinstance(p, dict):
            err(pw, "must be a mapping with at least 'name'.")
            continue
        pname = p.get("name")
        if not isinstance(pname, str) or not re.match(r"^[A-Za-z_]\w*$", pname):
            err(pw, "name: required, a bind-parameter name (letters, digits, _).")
            continue
        if pname in seen:
            err(pw, f"name: {pname!r} is declared twice.")
            continue
        seen.add(pname)
        out.append(_jsonable(p, pw, err))
    return out


def _widget(w, where, dataset_keys, err):
    if not isinstance(w, dict):
        err(where, "must be a mapping.")
        return None
    _unknown_keys(w, WIDGET_KEYS, where, err)
    wtype = w.get("type")
    ok = True
    if wtype not in Widget.Kind.values:
        err(where, f"type: must be one of {', '.join(Widget.Kind.values)} (got {wtype!r}).")
        ok = False
    title = w.get("title") or ""
    if not isinstance(title, str) or len(title) > NAME_MAX:
        err(where, f"title: must be text of at most {NAME_MAX} characters.")
        ok = False
    ds_key = w.get("dataset")
    if ds_key in (None, ""):
        ds_key = None
        if ok and wtype not in DATASETLESS_WIDGETS:
            err(where, f"dataset: required for a {wtype} widget.")
            ok = False
    elif ds_key not in dataset_keys:
        err(where, f"dataset: {ds_key!r} is not a dataset key of this pack.")
        ok = False
    config = w.get("config") or {}
    if not isinstance(config, dict):
        err(where, "config: must be a mapping.")
        ok, config = False, {}
    config = _jsonable(config, f"{where}.config", err)
    layout = dict(LAYOUT_DEFAULTS)
    raw_layout = w.get("layout") or {}
    if not isinstance(raw_layout, dict):
        err(where, "layout: must be a mapping of x, y, w, h.")
        ok = False
    else:
        for k, v in raw_layout.items():
            if k not in LAYOUT_DEFAULTS:
                err(where, f"layout: unknown key {k!r} (allowed: x, y, w, h).")
                ok = False
            elif isinstance(v, bool) or not isinstance(v, int) or v < (1 if k in "wh" else 0):
                err(where, f"layout.{k}: must be an integer "
                           f"{'>= 1' if k in 'wh' else '>= 0'} (got {v!r}).")
                ok = False
            else:
                layout[k] = v
    if not ok:
        return None
    return PackWidget(title=title, type=wtype, dataset_key=ds_key, config=config,
                      layout=layout)


def _jsonable(value, where, err):
    """Coerce YAML values to JSON: dates → ISO text; reject the rest loudly."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                err(where, f"key {k!r} is not text — quote it (YAML reads "
                           "on/off/yes/no as booleans).")
                continue
            out[k] = _jsonable(v, f"{where}.{k}", err)
        return out
    if isinstance(value, (list, tuple)):
        return [_jsonable(v, f"{where}[{i}]", err) for i, v in enumerate(value)]
    if isinstance(value, (_dt.datetime, _dt.date)):
        return value.isoformat()
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    err(where, f"unsupported value {value!r}.")
    return None


def _check_name_clashes(name, where, err):
    """A pack dataset owns the query and dataset of its name — refuse when
    that would be ambiguous or would change objects outside the pack."""
    queries = list(QueryDefinition.objects.filter(name=name))
    if len(queries) > 1:
        err(where, f"name: {len(queries)} existing queries are named {name!r}; "
                   "rename or delete the duplicates.")
    elif queries:
        others = sorted(queries[0].datasets.exclude(name=name)
                        .values_list("name", flat=True))
        if others:
            err(where, f"name: the existing query {name!r} also feeds "
                       f"{', '.join(repr(o) for o in others)}, which "
                       f"{'is' if len(others) == 1 else 'are'} not in this pack; "
                       "rename the pack dataset or that query.")
    count = Dataset.objects.filter(name=name).count()
    if count > 1:
        err(where, f"name: {count} existing datasets are named {name!r}; "
                   "rename or delete the duplicates.")


# ──────────────────────────────────────────────────────────────────────────
#  Applying
# ──────────────────────────────────────────────────────────────────────────

def apply_pack(pack: Pack, owner, recipients=None) -> ApplyResult:
    """Create or update the pack's objects by name. Run it inside a
    transaction (the seed command does, and rolls back for --dry-run).

    ``recipients`` (a list of addresses) sets the recipients of every pack
    report; None leaves existing reports' recipients and on/off state alone."""
    result = ApplyResult()
    for spec in pack.datasets:
        query, query_action = _upsert_query(spec, pack, owner)
        dataset, action = _upsert_dataset(spec, query, pack, owner,
                                          query_changed=query_action != "unchanged")
        result.datasets[spec.key] = dataset
        result.actions.append(("dataset", spec.name, action, ""))
    for spec in pack.dashboards:
        action, note = _upsert_dashboard(spec, pack, owner, result.datasets)
        result.actions.append(("dashboard", spec.name, action, note))
    for spec in pack.reports:
        action, note = _upsert_report(spec, owner, result.datasets[spec.dataset_key],
                                      recipients)
        result.actions.append(("report", spec.name, action, note))
    return result


def _apply_fields(obj, fields):
    changed = [k for k, v in fields.items() if getattr(obj, k) != v]
    for k in changed:
        setattr(obj, k, fields[k])
    return changed


def _share(obj, group) -> bool:
    """Make ``obj.shared_with`` exactly the group's members; True if changed.
    No group configured → sharing is left as it is."""
    if group is None:
        return False
    wanted = set(group.user_set.values_list("id", flat=True))
    if set(obj.shared_with.values_list("id", flat=True)) == wanted:
        return False
    obj.shared_with.set(wanted)
    return True


def _upsert_query(spec: PackDataset, pack: Pack, owner):
    fields = {
        "description": spec.description, "datasource_id": spec.datasource.id,
        "database": "", "mode": QueryDefinition.Mode.RAW, "raw_sql": spec.sql,
        "parameters": spec.parameters, "owner_id": owner.id,
        "visibility": pack.visibility,
    }
    query = QueryDefinition.objects.filter(name=spec.name).first()
    if query is None:
        query = QueryDefinition.objects.create(name=spec.name, **fields)
        _share(query, spec.share_group)
        return query, "created"
    changed = _apply_fields(query, fields)
    if changed:
        query.save()
    shared = _share(query, spec.share_group)
    return query, "updated" if (changed or shared) else "unchanged"


def _upsert_dataset(spec: PackDataset, query, pack: Pack, owner, *, query_changed):
    fields = {
        "description": spec.description, "query_id": query.id, "owner_id": owner.id,
        "visibility": pack.visibility, "refresh_interval": spec.refresh,
    }
    dataset = Dataset.objects.filter(name=spec.name).first()
    if dataset is None:
        # next_refresh_at stays empty: a scheduled dataset is due at once.
        dataset = Dataset.objects.create(name=spec.name, **fields)
        _share(dataset, spec.share_group)
        return dataset, "created"
    changed = _apply_fields(dataset, fields)
    if "refresh_interval" in changed or "query_id" in changed or query_changed:
        dataset.next_refresh_at = None  # re-schedule (a manual dataset stays manual)
    if changed or query_changed:
        dataset.save()
    shared = _share(dataset, spec.share_group)
    return dataset, "updated" if (changed or query_changed or shared) else "unchanged"


def _upsert_dashboard(spec: PackDashboard, pack: Pack, owner, datasets):
    fields = {"description": spec.description, "owner_id": owner.id,
              "visibility": pack.visibility, "filters": spec.filters}
    dashboard = Dashboard.objects.filter(name=spec.name).first()
    created = dashboard is None
    if created:
        dashboard = Dashboard.objects.create(name=spec.name, **fields)
        changed = []
    else:
        changed = _apply_fields(dashboard, fields)
        if changed:
            dashboard.save()

    desired = [
        (w.title, w.type, datasets[w.dataset_key].id if w.dataset_key else None,
         w.config, w.layout["x"], w.layout["y"], w.layout["w"], w.layout["h"], order)
        for order, w in enumerate(spec.widgets)
    ]
    existing = [
        (w.title, w.widget_type, w.dataset_id, w.config, w.x, w.y, w.w, w.h, w.order)
        for w in dashboard.widgets.order_by("order", "id")
    ]
    replaced = desired != existing
    if replaced:
        dashboard.widgets.all().delete()
        Widget.objects.bulk_create([
            Widget(dashboard=dashboard, title=title, widget_type=wtype,
                   dataset_id=dataset_id, config=config, x=x, y=y, w=w, h=h, order=order)
            for (title, wtype, dataset_id, config, x, y, w, h, order) in desired
        ])
        if not created and not changed:
            dashboard.save(update_fields=["updated_at"])
    shared = _share(dashboard, spec.share_group)
    note = f"{len(desired)} widget{'s' if len(desired) != 1 else ''}"
    if created:
        return "created", note
    if replaced:
        note += ", replaced"
    return ("updated" if (changed or replaced or shared) else "unchanged"), note


def _upsert_report(spec: PackReport, owner, dataset, recipients):
    """A pack report is a scheduled, audited export of a pack dataset (one email and one
    audit row per recipient). Without ``recipients`` a NEW report is created inactive, and an
    existing one keeps the recipients and on/off state an administrator gave it."""
    fields = {"dataset_id": dataset.id, "owner_id": owner.id, "schedule": spec.schedule,
              "export_format": spec.export_format,
              "kind": DatasetReport.Kind.DATASET_CSV}  # legacy name: export_format picks the file
    if recipients is not None:
        fields.update(recipient_emails=", ".join(recipients), is_active=bool(recipients))
    report = DatasetReport.objects.filter(name=spec.name).first()
    if report is None:
        fields.setdefault("recipient_emails", "")
        fields.setdefault("is_active", False)
        report = DatasetReport.objects.create(name=spec.name, **fields)
        action = "created"
    else:
        changed = _apply_fields(report, fields)
        if changed:
            report.save()
        action = "updated" if changed else "unchanged"
    count = len([e for e in (report.recipient_emails or "").split(",") if e.strip()])
    if not count:
        state = "inactive: no recipients"
    elif not report.is_active:
        state = f"inactive, {count} recipient{'s' if count != 1 else ''}"
    else:
        state = f"{count} recipient{'s' if count != 1 else ''}"
    return action, f"{spec.schedule}, {spec.export_format}, {state}"
