"""The extractor side of the plugin boundary: a client, a loop, and the checks that prove it.

An extractor is a container that speaks `extractor-api/v1` (specs/05-extractor-contract.md). The
contract is the boundary — `openapi-extractor.json` in this repository describes it in full, and
an image that implements six HTTP calls needs nothing from here. This package exists because the
six calls have a shape worth getting right once:

    from se_extractor import ExtractorClient, run

    MANIFEST = {
        "id": "my-extractor",
        "version": "1.0.0",
        "api_version": "v1",
        "accepts": {"mime_types": ["image/*"]},
        "produces": ["metadata"],
    }

    def handle(job, context):
        data = context.client.read_input(job)
        ...                      # analyse it; check `context.cancelled` in long loops
        return None              # or a result envelope

    run(ExtractorClient(base_url, token), MANIFEST, handle)
"""

from se_extractor.client import (
    ContractError,
    ExtractorClient,
    ExtractorError,
    LeaseLost,
    Unavailable,
)
from se_extractor.loop import (
    Cancelled,
    Handler,
    JobContext,
    PermanentFailure,
    Worker,
    run,
)
from se_extractor.models import FileVersion, Heartbeat, Job, JobInput, Registration

__all__ = [
    "Cancelled",
    "ContractError",
    "ExtractorClient",
    "ExtractorError",
    "FileVersion",
    "Handler",
    "Heartbeat",
    "Job",
    "JobContext",
    "JobInput",
    "LeaseLost",
    "PermanentFailure",
    "Registration",
    "Unavailable",
    "Worker",
    "run",
]
