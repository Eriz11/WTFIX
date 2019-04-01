import json

from wtfix.core import decoders, encoders
from wtfix.message import admin


class TestRESTfulServiceApp:
    def test_get_status(self, api_app, unsync_event_loop):
        response = api_app.flask_app.get("/")

        assert response.status_code == 200

        result = json.loads(response.data)
        assert result["success"] is True
        assert result["message"] == "WTFIX REST API is up and running!"

    def test_get_send(self, api_app, unsync_event_loop):

        msg = admin.TestRequestMessage("TEST123")
        encoded_msg = encoders.to_json(msg)

        response = api_app.flask_app.post("/send", data={"message": encoded_msg})

        assert response.status_code == 200

        result = json.loads(response.data)
        assert result["success"] is True
        assert result["message"] == "Successfully added message to pipeline!"
        assert result["data"]["message"] == encoded_msg

        assert (
            decoders.from_json(result["data"]["message"]) == msg
        )  # Test idempotency while we're at it.
