import logging
from typing import List, Tuple
from fastapi import APIRouter, Depends, Request, UploadFile, HTTPException, status
from fastapi.openapi.models import APIKey
from intellireading.api_server.routers.authentication import get_api_key, is_turnstile_valid
from opentelemetry import trace, metrics
from opentelemetry.trace import Tracer
from intellireading.api_server.monitoring.instrumentation import (
    current_span_add_warning_event,
    current_span_set_attribute,
)
from werkzeug.utils import secure_filename
from intellireading.client.metaguiding import metaguide_epub_stream, metaguide_xhtml_stream


router = APIRouter(prefix="/metaguiding", tags=["metaguiding"])
# Get the logger for this module
_logger: logging.Logger = logging.getLogger(__name__)
# Creates a tracer from the global tracer provider
_tracer: Tracer = trace.get_tracer(__name__)
# Creates a meter from the global meter provider
_meter = metrics.get_meter(__name__)

# metric counters
_files_transformed_counter = _meter.create_counter(
    "metaguiding.files.transformed", description="Number of files transformed", unit="1"
)

_files_size_counter = _meter.create_counter(
    "metaguiding.files.size", description="Size of files transformed", unit="bytes"
)


# region ----------------- request id -----------------
def _get_request_id(request: Request):
    from uuid import uuid4

    # check if we have request_id in the request state. if not, log a warning
    if not hasattr(request.state, "request_id"):
        _logger.warning(
            "Request id not found in request state. This should not happen. "
            "Please check middleware configuration. "
            "Generating a request id to ensure logging consistency..."
        )
        request.state.request_id = str(uuid4())
    return request.state.request_id


# endregion


# region ------------------ exception handling ------------------
def _raise_http_exception(status_code: int, message: str):
    current_span_add_warning_event("exception", message)
    raise HTTPException(status_code=status_code, detail=message)


# endregion


# region ----------------- file validation -----------------
def _validate_content_type_and_extension(
    file: UploadFile, valid_content_types: List[str], valid_extensions: List[str]
) -> Tuple[str, str]:
    _filename = secure_filename(file.filename or "")
    _content_type = file.content_type or ""
    current_span_set_attribute("filename", _filename)
    current_span_set_attribute("content_type", _content_type)

    if _content_type not in valid_content_types:
        _raise_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            message=(
                f"Invalid content type {_content_type}. "
                "Please ensure that the file is using the correct content type for "
                "the operation you are calling. "
            ),
        )
    if "." not in _filename or _filename.rsplit(".", 1)[1].lower() not in valid_extensions:
        _raise_http_exception(
            status_code=status.HTTP_400_BAD_REQUEST,
            message=f"File {_filename} has invalid extension. Valid extensions: {valid_extensions}",
        )
    return _filename, _content_type


def _get_valid_xhtml(file: UploadFile) -> UploadFile:
    # we allow application/octet-stream because some browsers send this content type for
    # xhtml files when handling with zip files inside javascript
    _validate_content_type_and_extension(
        file,
        [
            "application/xhtml+xml",
            "text/html",
            "application/xhtml",
            "application/octet-stream",
        ],
        ["xhtml", "html"],
    )
    return file


def _get_valid_epub(file: UploadFile) -> UploadFile:
    _validate_content_type_and_extension(
        file,
        [
            "application/zip",
            "application/x-zip-compressed",
            "application/x-zip",
            "application/x-compressed-zip",
            "application/epub+zip",
            "application/x-epub+zip",
        ],
        ["epub", "kepub", "zip"],
    )

    def _check_zip_file(file: UploadFile):
        from zipfile import ZipFile, BadZipFile

        try:
            with ZipFile(file.file) as zip_file:
                # testzip() returns None if the file is valid
                # (else it returns the name of the first corrupt file)
                return zip_file.testzip() is None
        except BadZipFile:
            return False

    if not _check_zip_file(file):
        _raise_http_exception(
            status.HTTP_400_BAD_REQUEST,
            "Invalid file. The submitted file is not a valid epub file (Failed zip check).",
        )
    file.file.seek(0)
    return file


# endregion


# region ----------------- file processing -----------------
async def _process_file_request(request: Request, file: UploadFile, f):
    # this block is not exception handled as the exception should be handled
    # by the middleware pipeline

    from io import BytesIO

    with _tracer.start_as_current_span("_process_file_request"):
        _request_id = _get_request_id(request)
        _request_path = request.url.path

        # although we don't store the file, we'll use the secure_filename
        # function to sanitize the filename
        _filename = secure_filename(file.filename or "")
        _filesize = file.size or 0
        _content_type = file.content_type or ""

        # add metrics
        _files_transformed_counter.add(1, {"request_path": _request_path, "filename": _filename})
        _files_size_counter.add(_filesize, {"request_path": _request_path, "filename": _filename})

        _logger.debug(
            "Request id %s: Processing file %s with function %s",
            _request_id,
            _filename,
            f.__name__,
        )
        with _tracer.start_as_current_span(
            "processing file", set_status_on_exception=False, record_exception=False
        ):
            _output_stream: BytesIO = f(file.file)

        with _tracer.start_as_current_span("sending file"):
            _output_stream.seek(0)
            # TODO: move this code to a separate module (utils.py?) # pylint: disable=fixme
            from typing import Generator

            # this is a generator function that will be used to
            # stream the file content and using asyncio
            # this will make sure we don't hit a performance bottleneck when sending large files
            async def get_file_content(stream: BytesIO) -> Generator:  # type: ignore
                yield stream.read()  # type: ignore

            from fastapi.responses import StreamingResponse

            return StreamingResponse(
                get_file_content(_output_stream),
                status_code=201,
                media_type=_content_type,
                headers={"Content-Disposition": f'attachment; filename="{_filename}"'},
            )


# endregion

# region --------------------- ROUTES --------------------------------


@router.post("/xhtml/transform")
async def transform_xhtml(
    request: Request,
    api_key: APIKey = Depends(get_api_key),  # noqa: ARG001
    file: UploadFile = Depends(_get_valid_xhtml),
):
    """
    Transforms an xhtml file into a metaguided xhtml file.
    Requires a valid api key.
    """

    # Open the uploaded xhtml file
    return await _process_file_request(
        request,
        file,
        lambda xhtml_content: metaguide_xhtml_stream(xhtml_content),
    )


@router.post("/epub/transform")
async def transform_epub(
    request: Request,
    api_key: APIKey = Depends(get_api_key),  # noqa: ARG001
    file: UploadFile = Depends(_get_valid_epub),
):
    """
    Transforms an epub file into a metaguided epub file.
    Requires a valid api key.
    """

    return await _process_file_request(
        request,
        file,
        lambda epub_content: metaguide_epub_stream(epub_content),
    )


@router.post("/epub/transform/submit")
async def submit_epub(
    request: Request,
    file: UploadFile = Depends(_get_valid_epub),
    *,
    turstile_valid: bool = Depends(is_turnstile_valid),  # noqa: ARG001
):
    """
    Transforms an epub file into a metaguided epub file.
    Requires a valid turnstile.
    """

    return await _process_file_request(
        request,
        file,
        lambda epub_input: metaguide_epub_stream(epub_input),
    )


# endregion
