"""Domain constants for the Lexware voucherlist / voucher types."""

# Default voucher statuses for voucherlist queries (voucherStatus is required).
# 'overdue' is a derived status that cannot be combined with others on the API
# side, so it is intentionally excluded from the default — query it explicitly.
DEFAULT_VOUCHER_STATUSES = (
    "draft,open,paid,paidoff,voided,transferred,sepadebit,accepted,rejected"
)

# All voucher types accepted by /v1/voucherlist — used when the caller wants
# to look up a voucher by number without knowing its type up front.
ALL_VOUCHER_TYPES = (
    "salesinvoice,salescreditnote,purchaseinvoice,purchasecreditnote,"
    "invoice,downpaymentinvoice,creditnote,orderconfirmation,quotation,deliverynote"
)

# Lexware exposes both 'salesinvoice' (new API) and 'invoice' (legacy) as
# customer-facing invoice types; some accounts also use 'downpaymentinvoice'.
# When the user asks for an invoice, we look across all three.
INVOICE_LIKE_TYPES = "salesinvoice,invoice,downpaymentinvoice"

# Default voucher types for the generic `vouchers list` (sales + purchase docs).
DEFAULT_VOUCHER_TYPES = (
    "salesinvoice,salescreditnote,purchaseinvoice,purchasecreditnote"
)
