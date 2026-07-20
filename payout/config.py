"""Static configuration for the Tevi payout runner (ID + PH markets).

Values reconstructed from the payout-id / payout-ph skill behaviour. Keep this
file as the single source of truth for agency IDs, currency conversion, chat
routing and detection keywords.
"""

# --- Markets -----------------------------------------------------------------
# Each market maps to one withdrawal agency in the fiat warehouse.
#
# usdt_rate: local currency per 1 USDT, used to convert net local amounts into
# the USDT/VAI figure the owner prefunds (matches the skill's DEFAULT_IDR_PER_USDT).
# ID = 17100 (fixed, per payout-id skill).
#
# dynamic_rate_markup: when set (PH), the rate is NOT fixed — it is computed per
# run as avg(payload.payout_request.exchange_rate over the Open rows) + markup,
# matching the payout-ph skill (PHP_PER_USDT = avg(exchange_rate) + 0.1). The
# +0.1 buffers per-transaction rate drift so USDT paid always covers PHP face
# value. If usdt_rate is None AND no markup applies (or no exchange_rate is
# present in the payload), the batch falls back to the warehouse ``amount_usd``.
MARKETS = {
    "id": {
        "label": "Indonesia",
        "agency_name": "LUINDO Payout Agency",
        "agency_id": "1711465765",
        "currency": "IDR",
        "batch_prefix": "ID",
        "telegram_tool": "send_payout_id_pdf",
        "usdt_rate": 17100.0,
    },
    "ph": {
        "label": "Philippines",
        "agency_name": "PHP Payout",
        "agency_id": "3635488650",
        "currency": "PHP",
        "batch_prefix": "PH",
        "telegram_tool": "send_payout_ph_pdf",
        "usdt_rate": None,           # no fixed rate — computed dynamically per run
        "dynamic_rate_markup": 0.1,  # PHP/USDT = avg(exchange_rate) + 0.1 (payout-ph skill)
    },
}

# E-wallet requests store the beneficiary under different payload keys than bank
# requests. Method ids: ID 11 = Bank, 16 = E-Wallet; PH 10 = bank-style (incl.
# GCash). Used only for the display label — field selection is done by coalesce.
METHOD_LABELS = {"11": "Bank", "16": "E-Wallet", "10": "Bank"}

# --- Detection ---------------------------------------------------------------
# Holder names that match an e-wallet / bank keyword are almost always a
# mis-filled "account name" field and get flagged for manual review (they are
# NOT blocked automatically — reviewer decides).
SUSPICIOUS_NAME_KEYWORDS = [
    # Indonesia e-wallets / banks
    "dana", "gopay", "ovo", "shopeepay", "linkaja", "seabank", "jenius",
    "bca", "bri", "bni", "mandiri", "cimb", "permata", "bsi", "btn",
    # Philippines e-wallets / banks
    "gcash", "maya", "paymaya", "bdo", "bpi", "unionbank", "metrobank",
    "grabpay", "coins.ph",
    # Generic red flags
    "test", "admin", "n/a", "na", "-",
]

# A holder name shorter than this (after trimming) is flagged as suspicious.
MIN_HOLDER_NAME_LEN = 3
