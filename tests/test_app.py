
from app import app, socketio

def test_home_page():
    client = app.test_client()
    response = client.get("/")
    assert response.status_code == 200

def test_health_endpoint():
    client = app.test_client()
    response = client.get("/health")
    assert response.status_code == 200

def test_metrics_endpoint():
    client = app.test_client()
    response = client.get("/metrics")
    assert response.status_code == 200
    assert b"devguard_active_connections" in response.data

def test_socketio_connection():
    client = socketio.test_client(app)
    assert client.is_connected()
    client.disconnect()
