# Spend Caps API

HTTP API for creating per-project spend caps for customers on top of the
[Cloud Billing Budget API](https://cloud.google.com/billing/docs/how-to/budget-api-overview).

A GCP budget on its own only sends alerts. To make it a real cap, this service follows Google's
[disable billing with notifications](https://cloud.google.com/billing/docs/how-to/disable-billing-with-notifications)
pattern:

```
POST /spend-caps ──► Budget (project filter, amount, thresholds, Pub/Sub topic)
                          │  Cloud Billing publishes cost updates several times a day
                          ▼
                  Pub/Sub topic ──push──► POST /v1/budget-notifications
                                              │ cost >= budget amount
                                              ▼
                                  unlink project from billing account
```

> **Warning:** disabling billing stops all paid services in the project, and some resources can be
> deleted after that. Cost data lags by hours, so actual spend can go over the cap. Re-enabling
> billing is a manual step (relink the project to the billing account).

## Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/billing-accounts/{ba}/spend-caps` | Create a cap for a project (409 if the project already has a budget) |
| `GET` | `/v1/billing-accounts/{ba}/spend-caps` | List budgets on the billing account |
| `GET` | `/v1/billing-accounts/{ba}/spend-caps/{id}` | Get one cap |
| `PATCH` | `/v1/billing-accounts/{ba}/spend-caps/{id}` | Change `amount`, `alert_thresholds`, `enforce` |
| `DELETE` | `/v1/billing-accounts/{ba}/spend-caps/{id}` | Delete the cap (budget) |
| `POST` | `/v1/budget-notifications` | Pub/Sub push target, enforces caps |
| `GET` | `/healthz` | Health check |

Interactive docs: `/docs`.

Create request:

```json
{
  "project_id": "acme-prod",
  "amount": "1500.00",
  "currency_code": "PLN",
  "display_name": "ACME prod cap",
  "alert_thresholds": [0.5, 0.9, 1.0],
  "enforce": true
}
```

Only `project_id` and `amount` are required. `currency_code` defaults to the billing account currency
and must match it if given. The budget is monthly and counts cost after credits.
`enforce: false` creates an alert-only budget (email to billing admins, no Pub/Sub, no billing cutoff).

Billing is disabled only when all of these are true:

- the notification reports `costAmount >= budgetAmount`
- the budget's Pub/Sub topic is the configured `SPEND_CAP_PUBSUB_TOPIC`
- the project is still linked to the same billing account and billing is enabled

## Configuration

| Env var | Description |
|---|---|
| `SPEND_CAP_PUBSUB_TOPIC` | `projects/<ops-project>/topics/<topic>`. Required for `enforce: true` |

Authentication uses Application Default Credentials.

## Deployment (Cloud Run)

```bash
PROJECT=<ops-project>  REGION=europe-central2  BA=<billing-account-id>
SA=spend-caps@$PROJECT.iam.gserviceaccount.com

gcloud services enable billingbudgets.googleapis.com cloudbilling.googleapis.com pubsub.googleapis.com run.googleapis.com --project $PROJECT
gcloud iam service-accounts create spend-caps --project $PROJECT
gcloud pubsub topics create spend-caps --project $PROJECT

# create/update/delete budgets
gcloud billing accounts add-iam-policy-binding $BA --member serviceAccount:$SA --role roles/billing.costsManager
# unlink projects from billing (grant on the org or the customers' folder)
gcloud organizations add-iam-policy-binding <org-id> --member serviceAccount:$SA --role roles/billing.projectManager
# attaching a topic to a budget requires pubsub.topics.setIamPolicy on it
gcloud pubsub topics add-iam-policy-binding spend-caps --project $PROJECT --member serviceAccount:$SA --role roles/pubsub.admin

gcloud run deploy spend-caps --source . --region $REGION --project $PROJECT \
  --service-account $SA --no-allow-unauthenticated \
  --set-env-vars SPEND_CAP_PUBSUB_TOPIC=projects/$PROJECT/topics/spend-caps

URL=$(gcloud run services describe spend-caps --region $REGION --project $PROJECT --format 'value(status.url)')
gcloud run services add-iam-policy-binding spend-caps --region $REGION --project $PROJECT \
  --member serviceAccount:$SA --role roles/run.invoker
gcloud pubsub subscriptions create spend-caps-push --project $PROJECT --topic spend-caps \
  --push-endpoint "$URL/v1/budget-notifications" --push-auth-service-account $SA
```

The service is private (`--no-allow-unauthenticated`). Callers need `roles/run.invoker` and send an
identity token:

```bash
curl -X POST "$URL/v1/billing-accounts/$BA/spend-caps" \
  -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
  -H 'Content-Type: application/json' \
  -d '{"project_id": "acme-prod", "amount": 1500}'
```

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
SPEND_CAP_PUBSUB_TOPIC=projects/x/topics/y .venv/bin/uvicorn app.main:app --reload
```
