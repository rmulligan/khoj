import logging
from typing import List

import requests
import tqdm
from sentence_transformers import CrossEncoder, SentenceTransformer
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)
from torch import nn

from khoj.utils.helpers import get_device, merge_dicts
from khoj.utils.rawconfig import SearchResponse

logger = logging.getLogger(__name__)


class EmbeddingsModel:
    def __init__(
        self,
        model_name: str = "thenlper/gte-small",
        embeddings_inference_endpoint: str = None,
        embeddings_inference_endpoint_api_key: str = None,
        query_encode_kwargs: dict = {},
        docs_encode_kwargs: dict = {},
        model_kwargs: dict = {},
    ):
        default_query_encode_kwargs = {"show_progress_bar": False, "normalize_embeddings": True}
        default_docs_encode_kwargs = {"show_progress_bar": True, "normalize_embeddings": True}
        self.query_encode_kwargs = merge_dicts(query_encode_kwargs, default_query_encode_kwargs)
        self.docs_encode_kwargs = merge_dicts(docs_encode_kwargs, default_docs_encode_kwargs)
        self.model_kwargs = merge_dicts(model_kwargs, {"device": get_device()})
        self.model_name = model_name
        self.inference_endpoint = embeddings_inference_endpoint
        self.api_key = embeddings_inference_endpoint_api_key
        self.embeddings_model = SentenceTransformer(self.model_name, **self.model_kwargs)

    def inference_server_enabled(self) -> bool:
        return self.api_key is not None and self.inference_endpoint is not None

    def embed_query(self, query):
        if self.inference_server_enabled():
            return self.embed_with_api([query])[0]
        return self.embeddings_model.encode([query], **self.query_encode_kwargs)[0]

    @retry(
        retry=retry_if_exception_type(requests.exceptions.HTTPError),
        wait=wait_random_exponential(multiplier=1, max=10),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(logger, logging.DEBUG),
    )
    def embed_with_api(self, docs):
        payload = {"inputs": docs}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(self.inference_endpoint, json=payload, headers=headers)
            response.raise_for_status()
        except requests.exceptions.HTTPError as e:
            logger.error(
                f" Error while calling inference endpoint {self.inference_endpoint} with error {e}, response {response.json()} ",
                exc_info=True,
            )
            raise e
        return response.json()["embeddings"]

    def embed_documents(self, docs):
        if self.inference_server_enabled():
            if "huggingface" not in self.inference_endpoint:
                logger.warning(
                    f"Unsupported inference endpoint: {self.inference_endpoint}. Only HuggingFace supported. Generating embeddings on device instead."
                )
                return self.embeddings_model.encode(docs, **self.docs_encode_kwargs).tolist()
            # break up the docs payload in chunks of 1000 to avoid hitting rate limits
            embeddings = []
            with tqdm.tqdm(total=len(docs)) as pbar:
                for i in range(0, len(docs), 1000):
                    docs_to_embed = docs[i : i + 1000]
                    generated_embeddings = self.embed_with_api(docs_to_embed)
                    embeddings += generated_embeddings
                    pbar.update(1000)
            return embeddings
        return self.embeddings_model.encode(docs, **self.docs_encode_kwargs).tolist() if docs else []


class CrossEncoderModel:
    def __init__(
        self,
        model_name: str = "mixedbread-ai/mxbai-rerank-xsmall-v1",
        cross_encoder_inference_endpoint: str = None,
        cross_encoder_inference_endpoint_api_key: str = None,
    ):
        self.model_name = model_name
        self.cross_encoder_model = CrossEncoder(model_name=self.model_name, device=get_device())
        self.inference_endpoint = cross_encoder_inference_endpoint
        self.api_key = cross_encoder_inference_endpoint_api_key

    def inference_server_enabled(self) -> bool:
        return self.api_key is not None and self.inference_endpoint is not None

    def predict(self, query, hits: List[SearchResponse], key: str = "compiled"):
        if self.inference_server_enabled() and "huggingface" in self.inference_endpoint:
            target_url = f"{self.inference_endpoint}"
            payload = {"inputs": {"query": query, "passages": [hit.additional[key] for hit in hits]}}
            headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
            response = requests.post(target_url, json=payload, headers=headers)
            return response.json()["scores"]

        cross_inp = [[query, hit.additional[key]] for hit in hits]
        cross_scores = self.cross_encoder_model.predict(cross_inp, activation_fct=nn.Sigmoid())
        return cross_scores
