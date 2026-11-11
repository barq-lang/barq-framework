# Changelog

## 0.21.0-alpha — 2026-11-11

- A `barq.pkg` manifest, so barq 0.21's package manager can install it into a project: `barq install barq-lang/barq-framework`.

## 0.20.0-alpha — 2026-11-11

- `barq new` (barq 0.20.0) gets the framework's starter files: `app/Providers/AppServiceProvider.bq`, the `User` model, a `create_users_table` migration, `UserFactory` and `DatabaseSeeder` (left out for `--console`; with `--auth` the User model and table come from barq-auth).

## 0.18.0-alpha — 2026-11-11

First release as its own repository (split out of the barq repo, where it was built in v0.9–v0.17). Needs barq 0.18.0-alpha or later. See the barq [changelog](https://github.com/barq-lang/barq/blob/main/CHANGELOG.md) for its history.
