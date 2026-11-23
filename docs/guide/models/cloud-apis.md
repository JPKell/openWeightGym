<!--docs
title = "Cloud APIs"
summary = "Offer provider models through separate configured accounts."
order = 50
covers = ["/cloud-apis"]
-->
# Cloud APIs

Offer provider models through separate configured accounts.

## Before you start

Use an account with [provider management](/docs/system/abilities#ability-manage-hosted). Disabled platform services may hide media and download choices.

## Do it

1. Open **Cloud APIs** and add a **Unique account name** with its **API endpoint**. Select **Add account & continue to key**.
2. Install the account's key under **02 · API key**. The saved credential is never displayed; replacement applies to new requests.
3. Under **03 · Models**, use **Discover models** or fill **Our name**, **Service model ID**, **Price in** and **Price out**, then **Save models**.

## Options

**04 · Spending & removal** contains **Daily spending limit** and **Remove account**. Separate accounts can share an endpoint while keeping their own key, model names and spending policy. Removing an account also deletes its installed key, so a later account given the same name starts without one. Removing an openWeightGym account configuration does not delete the provider account.

## Limits and troubleshooting

Use the full provider base address, including its API prefix where required. A discovery failure does not prove a credential is valid. Provider limits and billing remain separate from local spending policy. Caller access also needs [hosted-model permission](/docs/system/abilities#ability-hosted); configuration needs [provider management](/docs/system/abilities#ability-manage-hosted).

## Related

[Roles and routing](/docs/model-guides/roles) · [Settings reference](/docs/operations/settings-reference)

[Shared form controls and confirmations](/docs/getting-started/navigation#forms-and-confirmations)
