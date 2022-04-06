"""
upload_handler.py: Alternate implementation of the server-side handler that receives chunked file uploads from the
 Dash uploader component.

@author: sruffner
@created: 07dec2021
"""
import os
import traceback

from flask import request, abort
from dash_uploader.httprequesthandler import BaseHttpRequestHandler, get_chunk_name

from config.app_logging import get_application_logger


class UploadHandler(BaseHttpRequestHandler):
    """
    A replacement for the server-side handler provided by the dash_uploader package. This packages use ResumableJS
    on the client side to upload a file in chunks. BaseHttpRequestHandler, upon receiving the last chunk, reassembles
    the original file from the chunks. However, for our usage, we need to upload very large (5-10GB) session archive
    files, and it takes minutes to reassemble the file from the individual chunks. Also, with multiple GUnicorn
    worker processes handling requests, it's possible that multiple workers could attempt the "reassemble" step,
    which could cause problems.

    Instead, this replacement handler merely writes the chunk and does nothing else. The chunked archive is
    reassembled by the background process that is responsible for preprocessing that archive.
    """
    def __init__(self, server, upload_folder, use_upload_id):
        super().__init__(server=server, upload_folder=upload_folder, use_upload_id=use_upload_id)

    def post(self):
        try:
            chunk_number = request.form.get("resumableChunkNumber", default=1, type=int)
            file_name = request.form.get("resumableFilename", default="error", type=str)
            resumable_id = request.form.get("resumableIdentifier", default="error", type=str)
            upload_id = request.form.get("upload_id", default="", type=str)

            # get the chunk data
            chunk_data = request.files["file"]

            # make our temp directory
            temp_root = self.get_temp_root(upload_id)
            temp_dir = os.path.join(temp_root, resumable_id)
            if not os.path.isdir(temp_dir):
                os.makedirs(temp_dir)

            # save the chunk data
            chunk_name = get_chunk_name(file_name, chunk_number)
            chunk_file = os.path.join(temp_dir, chunk_name)

            chunk_data.save(chunk_file)
            return file_name
        except Exception:
            get_application_logger().error(traceback.format_exc())
            abort(500, "Error on server")
