"""Pinned CPU inference: multilingual E5 mean pooling and an independent multilingual cross-encoder."""

from functools import lru_cache

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

from . import QueryRejected, RetrievalUnavailable
from .assets import verify
from .config import ENCODER, RERANKER

BATCH = 8


class Network:
    def __init__(self, spec):
        directory = verify(spec)
        try:
            self.tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
            self.tokenizer.no_truncation()
            pad_id = self.tokenizer.token_to_id("<pad>")
            if pad_id is None:
                pad_id = self.tokenizer.token_to_id("[PAD]")
            if pad_id is None:
                raise ValueError("missing pad token")
            self.tokenizer.enable_padding(pad_id=pad_id)
            options = ort.SessionOptions()
            options.intra_op_num_threads = 1
            options.inter_op_num_threads = 1
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            filename = next(name for name in spec["files"] if name.endswith(".onnx"))
            self.session = ort.InferenceSession(
                str(directory / filename), sess_options=options, providers=["CPUExecutionProvider"]
            )
            self.inputs = {item.name for item in self.session.get_inputs()}
        except Exception as error:
            raise RetrievalUnavailable("source_search_model_load_failed") from error

    def infer(self, encoded):
        if any(len(row.ids) > 512 for row in encoded):
            raise RetrievalUnavailable("source_search_model_context_exceeded")
        feeds = dict(
            input_ids=np.array([row.ids for row in encoded], dtype=np.int64),
            attention_mask=np.array([row.attention_mask for row in encoded], dtype=np.int64),
            token_type_ids=np.array([row.type_ids for row in encoded], dtype=np.int64),
        )
        try:
            values = self.session.run(
                None, {key: value for key, value in feeds.items() if key in self.inputs}
            )[0]
        except Exception as error:
            raise RetrievalUnavailable("source_search_inference_failed") from error
        if not np.isfinite(values).all():
            raise RetrievalUnavailable("source_search_nonfinite_model_output")
        return values, feeds["attention_mask"]


class Models:
    def __init__(self):
        self.encoder, self.reranker = Network(ENCODER), Network(RERANKER)
        self.offset_tokenizer = Tokenizer.from_str(self.encoder.tokenizer.to_str())
        self.offset_tokenizer.no_padding()

    def offsets(self, text):
        # Independent tokenizer: padding must not alter the source's chunk boundaries.
        return self.offset_tokenizer.encode(text, add_special_tokens=False).offsets

    def query(self, text):
        tokenizer = self.encoder.tokenizer
        if len(tokenizer.encode("query: " + text).ids) > 128:
            raise QueryRejected("source_search_query_exceeds_128_tokens")
        return self.embed([text], prefix="query: ")[0]

    def embed(self, texts, *, prefix="passage: "):
        results = []
        for offset in range(0, len(texts), BATCH):
            encoded = self.encoder.tokenizer.encode_batch(
                [prefix + text for text in texts[offset : offset + BATCH]]
            )
            values, mask = self.encoder.infer(encoded)
            if values.ndim != 3 or values.shape[2] != 384:
                raise RetrievalUnavailable("source_search_embedding_shape_mismatch")
            pooled = (values * mask[:, :, None]).sum(axis=1) / mask.sum(axis=1)[:, None]
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            if np.any(norms <= 0):
                raise RetrievalUnavailable("source_search_zero_embedding")
            results.extend((pooled / norms).astype(np.float32))
        return np.asarray(results, dtype=np.float32).reshape(-1, 384)

    def rerank(self, query, texts):
        scores = []
        for offset in range(0, len(texts), BATCH):
            encoded = self.reranker.tokenizer.encode_batch(
                [(query, text) for text in texts[offset : offset + BATCH]]
            )
            values, _ = self.reranker.infer(encoded)
            if values.shape != (len(encoded), 1):
                raise RetrievalUnavailable("source_search_reranker_shape_mismatch")
            scores.extend((1 / (1 + np.exp(-np.clip(values[:, 0], -60, 60)))).tolist())
        return scores


@lru_cache(maxsize=2)
def models(cache_directory):
    # The directory is part of the identity, so changing installation roots never reuses another model.
    return Models()
