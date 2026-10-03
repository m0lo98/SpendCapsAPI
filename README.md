# Spend Caps API

HTTP API for creating [spend cap budgets](https://docs.cloud.google.com/billing/docs/how-to/budgets-spend-caps)
for customer projects through the Cloud Billing Budget API (`billingbudgets.googleapis.com/v1`, `spendCap` field).

When a spend cap triggers, Google blocks **new** usage of the capped service in the capped project
within minutes. Nothing is deleted and other services keep running. The block stays until the cap is
lifted manually (`:lift`) or the next month starts.

> Spend caps are in **Preview** (Pre-GA terms). Enforcement is not instantaneous, and any usage
> before it kicks in is billed. Set the amount slightly below the real limit.

## Preview constraints (enforced by this API)

| | |
|---|---|
| Scope | exactly one project and one service per cap |
| Services | `cloud-run` (152E-C115-5142), `cloud-run-functions` (29E7-DA93-CA13), `gemini-api` (AEFD-7695-64FA), `vertex-ai` (C7E2-9256-1C43, Agent Platform) |
| Period | monthly |
| Credits | excluded (`EXCLUDE_ALL_CREDITS`) |
| Alerts | email at 50 / 80 / 100% to billing admins and project owners |

Fixed commitments (CUDs, Provisioned Throughput) keep billing while a cap is enforced.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/billing-accounts/{ba}/spend-caps` | Create a cap (409 if the project already has a cap for that service) |
| `GET` | `/v1/billing-accounts/{ba}/spend-caps` | List spend caps on the billing account (regular budgets are skipped) |
| `GET` | `/v1/billing-accounts/{ba}/spend-caps/{id}` | Get one cap with its state |
| `PATCH` | `/v1/billing-accounts/{ba}/spend-caps/{id}` | Change `amount` (not allowed by GCP while `ENFORCED`) |
| `POST` | `/v1/billing-accounts/{ba}/spend-caps/{id}:lift` | Lift an enforced cap until the next month |
| `DELETE` | `/v1/billing-accounts/{ba}/spend-caps/{id}` | Delete the cap |
| `GET` | `/healthz` | Health check |

Interactive docs: `/docs`.

```json
POST /v1/billing-accounts/012345-6789AB-CDEF01/spend-caps
{
  "project_id": "acme-prod",
  "service": "gemini-api",
  "amount": "1500.00",
  "currency_code": "PLN",
  "display_name": "ACME Gemini cap"
}
```

`currency_code` and `display_name` are optional. The currency defaults to the billing account's and
must match it when given.

`state` in responses: `CONFIGURED` (active), `ENFORCED` (usage blocked), `AWAITING_NEXT_PERIOD`
(lifted, re-arms next month). `reconciling: true` means GCP is still applying a state change.

## Permissions

The caller identity (the Cloud Run service account) needs:

- `billing.budgets.configureSpendCap` on the billing account
- `billing.resourcebudgets.configureSpendCap` on each customer project

Predefined roles that grant this: Billing Account Administrator, or Billing Account Costs Manager
together with Project Editor/Owner on the customer projects (grant on the customers' folder).

## Deployment (Cloud Run)

```bash
PROJECT=<ops-project> REGION=europe-central2 BA=<billing-account-id>
SA=spend-caps@$PROJECT.iam.gserviceaccount.com

gcloud services enable billingbudgets.googleapis.com run.googleapis.com --project $PROJECT
gcloud iam service-accounts create spend-caps --project $PROJECT
gcloud billing accounts add-iam-policy-binding $BA --member serviceAccount:$SA --role roles/billing.costsManager
gcloud resource-manager folders add-iam-policy-binding <customers-folder-id> --member serviceAccount:$SA --role roles/editor

gcloud run deploy spend-caps --source . --region $REGION --project $PROJECT \
  --service-account $SA --no-allow-unauthenticated
```

Callers need `roles/run.invoker` and an identity token:

```bash
curl -X POST "$URL/v1/billing-accounts/$BA/spend-caps" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H 'Content-Type: application/json' \
  -d '{"project_id": "acme-prod", "service": "cloud-run", "amount": 1500}'
```

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
gcloud auth application-default login
GOOGLE_CLOUD_QUOTA_PROJECT=<project-with-billingbudgets-api> .venv/bin/uvicorn app.main:app --reload
```

With user credentials the Budget API needs a quota project that has `billingbudgets.googleapis.com` enabled.
On Cloud Run the service account's project is used.

The official Python client (`google-cloud-billing-budgets` 1.22.0) does not expose `spendCap` yet,
so `app/budgets_client.py` calls the REST API directly with `google-auth`.
