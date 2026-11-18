from statement_to_sheet.redact import redact_dataframe_columns

RAW_ACCOUNT = '123456789012'


def _row():
    return {'date': '2026-10-15', 'description': 'Coffee purchase', 'account': RAW_ACCOUNT}


def test_account_field_is_removed_by_default():
    cleaned, _ = redact_dataframe_columns([_row()])
    assert cleaned == [{'date': '2026-10-15', 'description': 'Coffee purchase'}]
    assert RAW_ACCOUNT not in repr(cleaned)


def test_keep_account_ref_only_keeps_last_four_digits():
    cleaned, _ = redact_dataframe_columns([_row()], keep_account_ref=True)
    assert cleaned[0]['account_ref'] == '****9012'
    assert 'account' not in cleaned[0]
    assert RAW_ACCOUNT not in repr(cleaned)
