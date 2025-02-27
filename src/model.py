"""OpenAI API handler for generating text for the README.md file."""

import asyncio
import time
from typing import Dict, List, Tuple

import httpx
import openai
from cachetools import TTLCache
from httpx import HTTPStatusError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import conf
import utils
from logger import Logger


class OpenAIHandler:
    """OpenAI API handler for generating text for the README.md file."""

    LOGGER = Logger(__name__)

    class RetryAfter(HTTPStatusError):
        """Custom exception for HTTP errors."""

        pass

    class OpenAIError(Exception):
        """Custom exception for OpenAI API errors."""

        pass

    def __init__(self, conf: conf.AppConfig):
        """Initialize the OpenAI API handler.

        Parameters
        ----------
        conf : conf.AppConfig
            Configuration constant values.
        """
        self.endpoint = conf.api.endpoint
        self.engine = conf.api.engine
        self.tokens = conf.api.tokens
        self.tokens_max = conf.api.tokens_max
        self.temperature = conf.api.temperature
        self.rate_limit = conf.api.rate_limit
        self.cache = TTLCache(maxsize=500, ttl=600)
        self.http_client = httpx.AsyncClient(
            http2=True,
            timeout=30,
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=100),
        )
        self.last_request_time = time.monotonic()
        self.rate_limit_semaphore = asyncio.Semaphore(self.rate_limit)

    async def code_to_text(self, ignore: dict, files: Dict[str, str],
                           prompt: str) -> Dict[str, str]:
        """Converts code to natural language text using large language models.

        Parameters
        ----------
        ignore : dict
            Files, directories, or file extensions to ignore.
        files : Dict[str, str]
            The repository files to convert to text.
        prompt : str
            The prompt to use for the OpenAI API calls.

        Returns
        -------
        Dict[str, str]
            Dictionary of file paths and their corresponding summaries.
        """
        tasks = []
        for path, contents in files.items():
            if not (
                all(idir not in path.parts for idir in ignore.get("directories", [])
                   ) and path.name not in ignore.get("files", []) and
                path.suffix not in ignore.get("extensions", [])
            ):
                self.LOGGER.warning(f"Ignoring file: {path}")
                continue

            prompt_code = prompt.format(contents)
            prompt_length = len(prompt_code.split())
            if prompt_length > self.tokens_max:
                exc = f"Prompt exceeds max token limit: {prompt_length}."
                tasks.append(asyncio.create_task(self.null_summary(path, exc)))
                self.LOGGER.debug(exc)
                continue

            tasks.append(
                asyncio.create_task(
                    self.generate_text(path, prompt_code, "file", self.tokens)
                )
            )
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return results

    async def chat_to_text(self, prompts: List[str]) -> List[str]:
        """Generate text using prompts and OpenAI's GPT-3.

        Parameters
        ----------
        prompts : List[str]
            The prompts to use for the OpenAI API calls.

        Returns
        -------
        List[str]
            A list of generated text.
        """
        if self.http_client.is_closed:
            self.http_client = httpx.AsyncClient()

        tasks = []
        for idx, prompt in enumerate(prompts):
            tokens = utils.adjust_max_tokens(self.tokens, prompt)
            tasks.append(
                asyncio.create_task(
                    self.generate_text(idx + 1, prompt, "prompt", tokens)
                )
            )

        results = []
        while tasks:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                result = await task
                results.append(result)
            tasks = pending

        response_list = [summary for _, summary in sorted(results)]
        return response_list

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=(
            retry_if_exception_type(Exception) |
            retry_if_exception_type(httpx.HTTPStatusError)
        ),
    )
    async def generate_text(self, index: str, prompt: str, type: str,
                            tokens: int) -> Tuple[str, str]:
        """Handles the request to the OpenAI API to generate text.

        Parameters
        ----------
        index
            Unique identifier for current prompt (i.e file path or index)
        prompt
            The prompt to send to the language model.
        type
            The type of prompt (i.e., code summary or general prompts).
        tokens
            The maximum number of tokens to generate.

        Returns
        -------
            Tuple containing the identifier and model's generated text.
        """
        if prompt in self.cache:
            return index, self.cache[prompt]

        # Add exponential backoff logic here
        elapsed_time = time.monotonic() - self.last_request_time
        if elapsed_time < 1 / self.rate_limit:
            await asyncio.sleep(1 / self.rate_limit - elapsed_time)

        try:
            async with self.rate_limit_semaphore:
                response = await self.http_client.post(
                    self.endpoint,
                    headers={"Authorization": f"Bearer {openai.api_key}"},
                    json={
                        "messages":
                            [
                                {
                                    "role": "system",
                                    "content": "You're a brilliant Tech Lead.",
                                },
                                {
                                    "role": "user",
                                    "content": prompt
                                },
                            ],
                        "model": self.engine,
                        "temperature": self.temperature,
                        "max_tokens": tokens,
                    },
                )
                response.raise_for_status()
                data = response.json()
                summary = data["choices"][0]["message"]["content"]
                summary = utils.format_sentence(summary)

                self.LOGGER.info(f"\nProcessing prompt: {index}\nResponse: {summary}")
                self.cache[prompt] = summary
                return index, summary

        except Exception as exc:
            self.LOGGER.error(f"Error while making API request to OpenAI:\n{str(exc)}")
            raise exc

        finally:
            self.last_request_time = time.monotonic()

    async def exception_handler(self, index: str, prompt: str, type: str,
                                exc) -> Tuple[str, str]:
        """Method to handle exceptions raised by the OpenAI API.

        Parameters
        ----------
        index : str
            Unique identifier for the prompt.
        prompt : str
            The prompt to use in the API request.
        type : str
            The type of prompt (i.e., code summary or general prompts).
        exc : Exception
            The exception raised by the API.

        Returns
        -------
        Tuple[str, str]
            Tuple of the unique index and the generated text summary.
        """
        self.LOGGER.error(f"Error while making API request to OpenAI:\n{str(exc)}")

        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code
            if status_code in [429, 500, 503, 504]:
                retry_after = int(exc.response.headers.get("Retry-After", 10))
                await asyncio.sleep(retry_after)
                return await self.generate_text(index, prompt, type, self.tokens)
            else:
                error_message = f"Error: {index}\n{status_code}"
        elif isinstance(exc, httpx.StreamClosed):
            error_message = (
                f"Stream reset while making request to OpenAI's API:\n{str(exc)}"
            )
        else:
            error_message = f"Error while making API request to OpenAI:\n{str(exc)}"

        return await self.null_summary(index, error_message)

    @staticmethod
    async def null_summary(index: str, summary: str) -> Tuple[str, str]:
        """Handles any exceptions raised while requesting the API."""
        return index, summary

    async def close(self):
        """Close the HTTP client."""
        await self.http_client.aclose()
