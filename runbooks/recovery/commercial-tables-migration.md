# Runbook: deploying migration `f0e1d2c3b4a5` (missing commercial tables)

**What it does:** brings the 12 commercial tables that were previously created
only by `app.commercial.bootstrap.ensure_tables()` (SQLAlchemy `create_all`)
under explicit Alembic control.

**Why this needed care:** every *already-provisioned* database created these 12
tables out-of-band via bootstrap on first boot, so a plain `CREATE TABLE` there
would fail with `table already exists`.

**This is now handled automatically.** `upgrade()` inspects the DB first: if the
tables already exist it no-ops (records the revision as applied); if they don't,
it creates them. So the standard deploy command is safe on **both** existing and
fresh databases — **no manual `alembic stamp` step required.**

## Standard deploy (all databases)

```bash
cd backend
alembic upgrade head
```

Verify:

```bash
alembic current   # should show f0e1d2c3b4a5 (head)
```

## Manual stamp (only if you need it)

If for any reason you want to mark the revision applied without running
`upgrade()` at all (e.g. a partial-state DB you're repairing by hand):

```bash
cd backend
alembic stamp f0e1d2c3b4a5
```

## Rollback

`downgrade()` drops all 12 tables (and their indexes). Only run this if you
intend to remove them:

```bash
alembic downgrade -1
```

⚠️ On an existing production DB this destroys billing-ledger, invoice,
checkout-session, webhook-receipt, fraud-signal, and manual-review data. Take a
backup first.

## Notes

- Verified on throwaway in-memory SQLite: `upgrade()` creates all 12 with
  correct indexes / unique constraints / FKs; `downgrade()` drops them cleanly.
- `bootstrap.ensure_tables()` is left in place as a harmless idempotent safety
  net (`create_all(checkfirst=True)`). It can be removed only after every
  environment is confirmed at revision `f0e1d2c3b4a5` or later.
- Never run `alembic upgrade` against a DB whose `DATABASE_URL` you have not
  confirmed — this repo's default `DATABASE_URL` points at production Supabase.
