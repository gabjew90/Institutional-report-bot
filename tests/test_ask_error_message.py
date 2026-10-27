"""The reply a member sees when /ask fails (2026-10-26: a deleted API key
told six askers their question "broke the model" and to rephrase)."""
from discord_bot.bot import _ask_error_message


def test_an_invalid_key_is_a_config_issue_not_the_question():
    err = ("400 INVALID_ARGUMENT. {'error': {'code': 400, 'message': 'API key not valid. "
           "Please pass a valid API key.', 'status': 'INVALID_ARGUMENT', 'details': "
           "[{'reason': 'API_KEY_INVALID'}]}}").lower()
    assert _ask_error_message(err) == "Config issue on the API key — admin needs to check it."


def test_a_bad_request_still_asks_to_rephrase():
    assert "rephrasing" in _ask_error_message("400 invalid_argument. request contains an invalid field")
