"""ClickHouse query builders for the payout runner.

Source of truth is the warehouse staging view
``tevi_warehouse.stg_fiat__withdrawals`` (matches the payout-id / payout-ph
skills). The view already applies ``FINAL`` + ``_peerdb_is_deleted = 0``, so it
returns exactly one current row per request — do NOT query the raw
``tevi_fiat.wd_tx`` table (that returns stale ``Open`` versions of requests that
have since flipped to ``Done`` and inflates the batch).

Key columns used here:
  - original_status : 'Open' (to bill), 'Fail' (credit), 'Done' (paid)
  - payout_method_id: '11' Bank / '16' E-Wallet (ID); '10' (PH, always bank-style)
  - amount_usd      : USD value of the request (informational)
  - payload JSON    : transaction.net_amount (local currency actually transferred)
                      transaction.billing_detail.* (beneficiary — see below)

Beneficiary lives under ``transaction.billing_detail`` in two shapes:
  Bank      : holder_name / account_number / bank (+ bank_name)
  E-Wallet  : account_name / phone_number    / wallet_name
The two shapes are mutually exclusive, so we coalesce across both.
"""

_STG = "tevi_warehouse.stg_fiat__withdrawals"

# Beneficiary coalesced across the bank and e-wallet field names.
_HOLDER = (
    "coalesce("
    "nullIf(JSONExtractString(payload,'transaction','billing_detail','holder_name'),''),"
    "nullIf(JSONExtractString(payload,'transaction','billing_detail','account_name'),''),'')"
)
_ACCT = (
    "coalesce("
    "nullIf(JSONExtractString(payload,'transaction','billing_detail','account_number'),''),"
    "nullIf(JSONExtractString(payload,'transaction','billing_detail','phone_number'),''),'')"
)
_BANK = (
    "coalesce("
    "nullIf(JSONExtractString(payload,'transaction','billing_detail','bank'),''),"
    "nullIf(JSONExtractString(payload,'transaction','billing_detail','bank_name'),''),"
    "nullIf(JSONExtractString(payload,'transaction','billing_detail','wallet_name'),''),'')"
)
_NET = "JSONExtractString(payload,'transaction','net_amount')"
_CONTACT = "JSONExtractString(payload,'payout_request','payout_config','contact_name')"


def open_requests_sql(agency_id: str) -> str:
    """All Open payout requests for one agency, oldest first."""
    return f"""
SELECT
  request_number,
  payout_method_id                                   AS method_id,
  {_NET}                                             AS net_local,
  amount_usd                                         AS usd,
  {_HOLDER}                                          AS holder,
  {_ACCT}                                            AS acct,
  {_BANK}                                            AS bank,
  nullIf(JSONExtractString(payload,'transaction','billing_detail','wallet_name'),'') AS wallet,
  nullIf(JSONExtractString(payload,'transaction','billing_detail','phone_number'),'') AS phone,
  {_CONTACT}                                         AS contact,
  toFloat64OrZero(JSONExtractString(payload,'payout_request','exchange_rate')) AS xr,
  formatDateTime(created_at,'%Y-%m-%d %H:%i')        AS created
FROM {_STG}
WHERE agency_id='{agency_id}' AND original_status='Open'
ORDER BY created_at ASC
""".strip()


def fail_credits_since_sql(agency_id: str, cutoff: str) -> str:
    """Fail rows since a cutoff timestamp (credits owed back to the owner).

    ``cutoff`` is a 'YYYY-MM-DD HH:MM:SS' string; a Fail flipped after the prior
    payout run is a prefund that was never disbursed and is deducted from the
    next transfer.
    """
    return f"""
SELECT
  request_number,
  payout_method_id                                   AS method_id,
  {_NET}                                             AS net_local,
  amount_usd                                         AS usd,
  {_HOLDER}                                          AS holder,
  {_ACCT}                                            AS acct,
  formatDateTime(updated_at,'%Y-%m-%d %H:%i')        AS failed_at
FROM {_STG}
WHERE agency_id='{agency_id}' AND original_status='Fail'
  AND updated_at > toDateTime('{cutoff}')
ORDER BY updated_at ASC
""".strip()


def vai_leaks_sql(agency_id: str) -> str:
    """VAI credit pre-flight: Done requests Tevi has NOT credited STAR/VAI for.

    A ``Done`` request means the agency already paid the user in fiat; Tevi must
    then transfer STAR (VAI) to the agency wallet to reimburse it. When that
    transfer is dropped (the 2026-07-02 settlement bug), ``vai_tx_id`` stays
    empty = money left the agency but VAI was never funded = a leak.

    This reads the RAW ``tevi_fiat.wd_tx`` (with FINAL) on purpose: the warehouse
    staging view backfills ``vai_tx_id``, which would hide a live leak.
    """
    return f"""
SELECT
  request_number,
  tx_id,
  user_alias,
  toString(amount)                             AS amount,
  country,
  payout_method_id,
  tx_status,
  formatDateTime(released_at,'%Y-%m-%d %H:%i') AS released_at,
  formatDateTime(created_on,'%Y-%m-%d %H:%i')  AS created_on
FROM tevi_fiat.wd_tx FINAL
WHERE _peerdb_is_deleted=0 AND agency_id='{agency_id}'
  AND status='Done' AND vai_tx_id=''
ORDER BY created_on ASC
""".strip()
