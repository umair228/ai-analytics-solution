-- Demo data for packs/example/pack.yaml (SQLite).
--   sqlite3 /tmp/example_orders.sqlite < packs/example/orders.sql
-- then register a SQLite data source named "Example Source" whose database
-- path is /tmp/example_orders.sqlite.
DROP TABLE IF EXISTS orders;
CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    status      TEXT NOT NULL,
    region      TEXT NOT NULL,
    amount      REAL NOT NULL,
    ordered_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
INSERT INTO orders (id, status, region, amount, ordered_at, updated_at) VALUES
    (1,  'open',      'North', 1200.0, '2026-09-02', '2026-11-22 21:00:00'),
    (2,  'open',      'South',  450.5, '2026-09-15', '2026-11-22 21:00:00'),
    (3,  'shipped',   'North',  980.0, '2026-09-21', '2026-11-22 21:00:00'),
    (4,  'shipped',   'East',   310.0, '2026-10-01', '2026-11-22 21:00:00'),
    (5,  'shipped',   'South', 2210.0, '2026-10-03', '2026-11-22 21:00:00'),
    (6,  'cancelled', 'West',   150.0, '2026-10-07', '2026-11-22 21:00:00'),
    (7,  'open',      'East',   640.0, '2026-10-19', '2026-11-22 21:00:00'),
    (8,  'shipped',   'West',  1730.0, '2026-10-28', '2026-11-22 21:00:00'),
    (9,  'open',      'North',  275.0, '2026-11-04', '2026-11-22 21:00:00'),
    (10, 'shipped',   'North',  860.0, '2026-11-11', '2026-11-22 21:00:00'),
    (11, 'cancelled', 'South',   95.0, '2026-11-16', '2026-11-22 21:00:00'),
    (12, 'open',      'West',   520.0, '2026-11-20', '2026-11-22 21:00:00');
