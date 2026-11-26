"""Keep routing/control unit tests independent of the planning/report contract.

The real pipeline is exercised in test_orchestration and its integration tests.
"""


class DirectTaskRunner:
    def __init__(self, client):
        self.client = client

    def run(self, task):
        return self.client.send(task.prompt, cancel_event=task.cancelled, **task.params)
