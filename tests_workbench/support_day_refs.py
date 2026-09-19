"""Complete frozen-source metadata for isolated daily projection fixtures."""
import hashlib
import io
import tarfile

from industry_workbench.models import json_bytes
from industry_workbench.day_integrity import INPUT_CONTRACT


def put_day_refs(store, inputs, result):
    body = b"# frozen isolated fixture\n"
    files = {"src/fixture.py": hashlib.sha256(body).hexdigest()}
    source = {"commit": None, "git_dirty": True, "files": files,
              "tree_sha256": hashlib.sha256(json_bytes(files)).hexdigest()}
    content = io.BytesIO()
    with tarfile.open(fileobj=content, mode="w:gz") as archive:
        entry = tarfile.TarInfo("src/fixture.py")
        entry.size = len(body)
        archive.addfile(entry, io.BytesIO(body))
    return {"input": store.put_json(inputs, "day_input"), "result": store.put_json(result, "day_result"),
            "input_contract_version": INPUT_CONTRACT, "result_source_sha256": source["tree_sha256"],
            "input_source": {"identity": {key: source[key] for key in ("commit", "git_dirty", "tree_sha256")},
                             "inventory": store.put_json(source, "source_identity"),
                             "snapshot": store.put_bytes(content.getvalue(), "source_snapshot_tar_gz")}}
