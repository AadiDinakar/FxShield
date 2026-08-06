from main import CircuitBreaker


def test_circuit_opens_after_threshold():
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)

    assert breaker.state == "closed"
    breaker.record_failure()
    assert breaker.state == "closed"
    breaker.record_failure()

    assert breaker.state == "open"
    assert breaker.allow_request() is False


def test_success_resets_breaker():
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    breaker.record_failure()
    assert breaker.state == "open"

    breaker.record_success()

    assert breaker.state == "closed"
    assert breaker.failure_count == 0
