"""Optional, offline-only learned embeddings from an operator-provisioned model.

No API credential, dynamic URL, implicit download, remote code or model fallback.
The app loads a fixed local directory; networked model acquisition is separate.
"""
from __future__ import annotations
import hashlib
import os
import threading
from pathlib import Path
from .errors import DomainError


class LocalSentenceEncoder:
    dimension = 512  # bge-small-zh-v1.5 is 512-dimensional; verified at load.

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        if not self.directory.is_dir() or not (self.directory/'model.safetensors').is_file():
            raise DomainError('EMBEDDING_NOT_CONFIGURED', 'Local safetensors model directory required', 503)
        manifest = hashlib.sha256()
        for path in sorted(self.directory.rglob('*')):
            if path.is_file() and path.suffix in {'.json', '.txt', '.safetensors'}:
                manifest.update(str(path.relative_to(self.directory)).encode())
                with path.open('rb') as stream:
                    for data in iter(lambda: stream.read(1024*1024), b''):
                        manifest.update(data)
        self.identity = 'local-bge-zh-v1:' + manifest.hexdigest()
        self._model = None
        self._lock = threading.Lock()

    def encode(self, texts, query=False):
        with self._lock:
            try:
                if self._model is None:
                    from sentence_transformers import SentenceTransformer
                    self._model = SentenceTransformer(str(self.directory), device='cpu',
                        trust_remote_code=False, local_files_only=True, token=False,
                        model_kwargs={'use_safetensors': True})
                    if self._model.get_sentence_embedding_dimension() != self.dimension:
                        self._model = None
                        raise ValueError('MODEL_DIMENSIONS')
                prefix = '为这个句子生成表示以用于检索相关文章：' if query else ''
                # No silent tokenizer truncation of reference evidence.
                for text in texts:
                    if len(self._model.tokenizer.encode(prefix+text, truncation=False)) > self._model.max_seq_length:
                        raise DomainError('EMBEDDING_TEXT_LIMIT', 'Reference chunk exceeds local model token limit', 422)
                return self._model.encode([prefix+t for t in texts], batch_size=8,
                    normalize_embeddings=True, show_progress_bar=False).tolist()
            except DomainError:
                raise
            except Exception as error:
                raise DomainError('EMBEDDING_UNAVAILABLE', 'Local embedding model could not be used', 503) from error


def configured_encoder():
    directory = os.getenv('PF_POLICY_EMBEDDING_DIR')
    return LocalSentenceEncoder(directory) if directory else None
