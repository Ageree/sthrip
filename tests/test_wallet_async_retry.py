"""Test that _acall retries on transient errors."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sthrip.wallet import MoneroWalletRPC


@pytest.mark.asyncio
async def test_acall_retries_on_connection_error():
    """_acall should retry up to 3 times on httpx connection errors."""
    import httpx

    wallet = MoneroWalletRPC(host="localhost", port=18082)

    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.json.return_value = {"result": {"balance": 100}}
    mock_response.raise_for_status = MagicMock()

    # Fail twice, succeed on third
    mock_client.post = AsyncMock(side_effect=[
        httpx.ConnectError("connection refused"),
        httpx.ConnectError("connection refused"),
        mock_response,
    ])

    # Mock _get_async_client to return our mock client
    wallet._get_async_client = AsyncMock(return_value=mock_client)

    result = await wallet._acall("get_balance", {"account_index": 0})
    assert result == {"balance": 100}
    assert mock_client.post.call_count == 3


@pytest.mark.asyncio
async def test_acall_raises_after_max_retries():
    """_acall should raise after exhausting retries."""
    import httpx

    wallet = MoneroWalletRPC(host="localhost", port=18082)

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
    wallet._get_async_client = AsyncMock(return_value=mock_client)

    with pytest.raises(httpx.ConnectError):
        await wallet._acall("get_balance")
