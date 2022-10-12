"""
repo.py: Implementation of the portal backing repository in a single bucket in AWS Single Storage Service (S3).

For cost-efficiency's sake, the portal backing repository is implemented using Amazon Web Services' S3. Duke IT has
provisioned an S3 bucket (actually two, one for production and one for development) for the Lisberger lab. Versioning
is disabled on the bucket (we don't need it), and access is granted for the operations we'll need: "s3:GetObject" for
downloading and querying file objects, "s3:PutObject" for uploading file objects, "s3:DeleteObject" to delete any
object in the bucket, and "s3:ListBucket" to traverse all objects in the bucket.

The portal server must supply the AWS Access Key ID and Secret in order to use the Boto3 S3 SDK to perform any actions
on the S3 bucket. These secrets are part of application configuration -- see config.AppConfig.

The S3 bucket can contain any number of file objects and is non-hiearchical storage. Each object is stored under a
string key. To create a file system-like folder hierarchy for the portal repository, we use path-like keys for all files
stored in it. All keys start with a forward slash, and addtional forward slashes separate the folders in the path-like
key. The portal repository contains 3 folders under the root: The /logs folder contains the backup of the database
operations log (and could be the location for error logs or similar files in the future). The /downloads folder is a
temporary location for data files prepared in response to download requests. After preparing a data file in reponse to
a download request, the file is uploaded to /downloads and a presigned URL generated so that the user can download the
file directly from the S3 bucket (without needing the requisite access credentials). The presigned URL expires after an
hour, and any file in the /downloads folder expires after 1 day (IAW a lifecycle configuration rule defined on the S3
bucket).

Finally, the /repo folder holds the ZIP archives for all experiment sessions that have been committed to the portal's
database. The key format for any particular session archive illustrates how the archives are organized under this
folder: /repo/<exp>/<subj>_<date>_<sfx>.zip, where: <exp> is the username of the experimenter, <subj> is the ID of the
experiment subject, <date> is the experiment date in the format 'YYYY-MM-DD', and <sfx> is the integer session suffix
(1-9). These four attributes form the primary key that uniquely identifies an experiment session in the database.

This module includes public methods to list repository contents, upload a file to or download a file from the
repository, delete a file in the repository, or obtain a presigned URL to a data file in the '/downloads' folder so
an external user can download that file directly from S3. It also includes the private methods that implement the
repository's storage in the provisioned S3 bucket. A __main__ entrypoint is available for test and diagnostic purposes.

@author: sruffner
@created: 15feb2022
"""
import json
import sys
import threading
import time
from pathlib import Path
from typing import Optional, List, Union, Dict, Any, Tuple, Callable

import requests
from boto3 import Session
from boto3.s3.transfer import TransferConfig

import config.app_logging as app_log
import config.config as app_cfg
from sglportalapi.util import MB, GB, size_with_units


def listing() -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """
    Retrieve a semi-structured listing of the contents of the portal repository.

    Returns:
        A dictionary of pseudo folder paths (eg, '/repo/username') in the repository that contain one or more files.
            Each key is a folder path, and the corresponding value is a list of file information objects, one per file
            in the path. Each file information object is a dictionary with the following keys: 'name' is the file name,
            'last_modified' is a datetime object indicating the object's creation time in S3, 'storage_class' is the
            object's S3 storage class, and 'size' is its total size in bytes. Each folder's file list is sorted in
            reverse chronological order by creation time. If the repository is empty, returns an empty dictionary. If
            an error occurs, returns None. Consult the application log for the error description.
    """
    contents = _bucket_contents(app_cfg.get_config().repo_bucket)
    if contents is None:
        return None
    folders: Dict[str, List[Dict[str, str]]] = dict()
    for o in contents:
        pos = o.key.rfind('/')
        if pos <= 0:
            folder_key = '/'
            file_name = o.key if pos == -1 else o.key[1:]
        else:
            folder_key = o.key[0:pos]
            file_name = o.key[pos+1:]
        if not (folder_key in folders):
            folders[folder_key] = list()
        folders[folder_key].append(
            dict(name=file_name, last_modified=o.last_modified, storage_class=o.storage_class, size=o.size))
    for folder_key in folders:
        folders[folder_key].sort(key=lambda x: x['last_modified'], reverse=True)
    return folders


def upload_file(file_path: Path, key: str, log_func: Union[bool, Callable] = True) -> bool:
    """
    Upload the specified file to the portal's backing repository.

    All files are stored in the repository under file path-like keys and must match one of these formats: '/logs/*' for
    log files, '/downloads/*' for experiment data files prepared in response to download requests, '/staging/*/*' for
    session archive ZIP files that are temporarily stored in the repository prior to being committed to the portal
    database, and '/repo/*/*' for the archive ZIPs of all committed experiment sessions.

    Args:
        file_path: File system path for the target file. Must exist.
        key: The S3 object key under which the file should be stored. Must satisfy portal constraints on key format.
        log_func: If this argument is True or is a callable function, a progress message is written to the portal app
            log to report 10% increments in progress. If the argument is a callable function, it is assumed to have the
            form `fn(pct: float)`, and the function is invoked after writing to the app log. (Of course, actual reported
            completion percentage will not necessarily be in exact 10% increments, depending on the total transfer size
            and update frequency. If the argument is False, progress is reported by overwriting a line on STDOUT each
            time the callback is invoked. Default = True.
    Returns:
        True if successful, False otherwise. Check application log for error desciription.
    Raises:
        ValueError: If object key violates expected format, or target file does not exist.
    """
    if not (_validate_key_format(key) and file_path.is_file()):
        raise ValueError("Bad repository file object key, or target file not found")
    return _upload_file_to_bucket(file_path, app_cfg.get_config().repo_bucket, key, log_func=log_func)


def _validate_key_format(key: str) -> bool:
    """
    Check that specified S3 object key conforms to the format expected for any file stored in the portal repository. By
    convention, the key must always start with a '/logs', '/downloads', '/staging', or '/repo'. Keys under 'repo' and
    'staging' will have 3 path parts (/repo/*/*.zip, /staging/*/*.zip), while keys under the other 2 folders have only
    2 path parts.

    Args:
        key: The object key.
    Returns:
        True if key conforms to the format expected of a file in the portal repository, else False.
    """
    ok = False
    try:
        parts = key.split('/')
        n, p1 = len(parts), parts[1]
        ok = (parts[0] == '') and (((n == 3) and (p1 in ['logs', 'downloads'])) or
                                   ((n == 4) and (p1 in ['repo', 'staging'])))
    except Exception:
        pass
    return ok


def download_file(key: str, dst: Path, log_func: Union[bool, Callable] = True) -> bool:
    """
    Download a file stored in the portal repository.

    Args:
        key: The file object key.
        dst: The file system destination path for the file object.
        log_func: If this argument is True or is a callable function, a progress message is written to the portal app
            log to report 10% increments in progress. If the argument is a callable function, it is assumed to have the
            form `fn(pct: float)`, and the function is invoked after writing to the app log. (Of course, actual reported
            completion percentage will not necessarily be in exact 10% increments, depending on the total transfer size
            and update frequency. If the argument is False, progress is reported by overwriting a line on STDOUT each
            time the callback is invoked. Default = True.
    Returns:
        True if successful; False otherwise. Error message is written to the portal application log.
    """
    return _download_file_from_bucket(app_cfg.get_config().repo_bucket, key, dst, log_func=log_func)


def read_text_file(key: str) -> Optional[str]:
    """
    Read the contents of a text file in the portal repository.

    Args:
        key: The file object key.

    Returns:
        The text file's content. Returns None if file not found in repository, or some other error occurs.
    """
    try:
        session = _aws_session()
        s3_resource = session.resource('s3')
        return s3_resource.Object(app_cfg.get_config().repo_bucket, key).get()["Body"].read().decode('utf-8')
    except Exception:
        app_log.get_application_logger().error(f"Failed to read text file at {key} in S3 repo", exc_info=True)
        return None


def download_url_for(key: str) -> Optional[str]:
    """
    Generate a URL by which a data file previously prepared in response to an experiment data download request may be
    downloaded from the portal repository.

    Data files prepared in response to a download request are stored for 1 day in the '/downloads' node in the
    repository. Since the repository is maintained in a private Amazon S3 bucket, a presigned URL must be supplied so
    that the external user that made the request can download the file directly to their machine.

    This method may not be used to generate a download URL for files elsewhere in the portal repository

    Args:
        key: The file object key.
    Returns:
        The URL string. The URL will expire in 1 hour. Returns None if an error occurs (consult application logs).
    Raises:
        ValueError: If key does not start with '/downloads'.
    """
    if not key.startswith('/downloads'):
        raise ValueError("Download URL only available for files in the /downloads folder!")
    return _presigned_url_for_file(app_cfg.get_config().repo_bucket, key)


def file_size(key: str) -> int:
    """
    Return the size of a file stored in the portal repository.

    Args:
        key: The file object's key.
    Returns: The file's size in bytes. Returns 0 if file not found or an internal error occurred.
    """
    return _file_size_in_bucket(app_cfg.get_config().repo_bucket, key)


def delete_file(key: str) -> bool:
    """
    Permanently delete a file stored in the portal repository

    Args:
        key: The file object's key.
    Returns:
        True if successful or object not found; False otherwise. Error message is written to the portal application log.
    """
    return _delete_file_in_bucket(app_cfg.get_config().repo_bucket, key)


def initialize_multipart_upload(file_sz: int, key: str) -> Tuple[bool, str, int, List[str]]:
    """
    Initiate a multipart upload to the portal repository.

    Use a multipart upload to push large files directly to the portal repository without first uploading them to the
    portal server itself. This method assigns an upload ID to the multipart upload and prepares a list of presigned URLs
    for uploading the file parts in order.

    Once a multipart upload is initiated on S3, it is imperative that the operation is either aborted (if an error has
    occurred) or completed (so that the uploaded file chunks are reconstituted into the original file). Otherwise, any
    uploaded parts will remain in S3. The upload ID is used to identify the multipart upload to abort or complete.
    As each part is uploaded, the part number and ETag must be saved, as this information must be supplied to S3 in
    order to complete the upload. The following code snippet suggests how this may be done, using the Python requests
    library to upload the file chunks::

        ok, upload_id, chunk_size, urls = initialize_multipart_upload(file_sz, key)
        for num, url in enumerate(urls):
            part = num + 1
            file_data = f.read(chunk_size)
            res = requests.put(url, data=file_data)
            if res.status_code != 200:
                abort_multipart_upload(key, upload_id)
                return None
            etag = res.headers['ETag']
            parts.append((etag, part))
        parts_list = [{'ETag': eval(x), 'PartNumber': int(y)} for x,y in parts]
        finish_multipart_upload(key, upload_id, parts_list)

    Note the calls to the associated methods `abort_multipart_upload` and `finish_multipart_upload`.

    The method only supports uploading a file >= 10MB in size. (For a smaller file, acquire a single presigned URL to
    upload the entire file in one transfer.) Chunk size will depend on the total file size, but will  max out at 100MB
    for file uploads of 300MB or more. The presigned URLs will expire in one hour, so the file must be uploaded in its
    entirety within that time frame.

    Args:
        file_sz: The size of the file object to be uploaded. Must be >= 10MB.
        key: The S3 object key under which the file should be stored. Must satisfy portal constraints on key format.
    Returns:
        A 4-tuple (S, U, K, L). If an error occurred, S=False and U is an error message. Otherwise, U is the upload ID,
            K is the file chunk size in bytes (use for all file chunks except the last), and L is the list of presigned
            part upload URLs.
    """
    # TODO: Validate key
    if file_sz < 10*MB:
        return False, "File size is too small for multipart upload", -1, []
    logger = app_log.get_application_logger()
    try:
        session = _aws_session()
        bucket_name = app_cfg.get_config().repo_bucket
        s3_client = session.client('s3')
        response = s3_client.create_multipart_upload(Bucket=bucket_name, Key=key)
        upload_id = response['UploadId']
        chunk_size = int(file_sz/2) if file_sz < 50*MB else (100*MB if file_sz >= 300*MB else 50*MB)
        num_parts = int(file_sz/chunk_size) + 1
        presigned_urls: List[str] = []
        for part_num in range(1, num_parts+1):
            url = s3_client.generate_presigned_url(
                ClientMethod='upload_part',
                Params={'Bucket': bucket_name, 'Key': key, 'UploadId': upload_id, 'PartNumber': part_num},
                ExpiresIn=3600
            )
            presigned_urls.append(url)
        logger.info(f"Initialized multipart upload of {file_sz/MB:.1f} file object in {num_parts} parts to "
                    f"{bucket_name}:{key}; upload ID = {upload_id}")
        return True, upload_id, chunk_size, presigned_urls
    except Exception as e:
        logger.error(f"Failed to initiate multipart upload: {str(e)}")
        return False, str(e), -1, []


def abort_multipart_upload(key: str, upload_id: str) -> bool:
    """
    Abort a previously started multipart upload to the portal repository. See `initialize_multipart_upload`.

    Args:
        key: The destination key for the multipart upload.
        upload_id: The multipart upload ID.
    Returns:
        True if successful, False otherwise. See application log for error description.
    """
    logger = app_log.get_application_logger()
    try:
        s3_client = _aws_session().client('s3')
        bucket_name = app_cfg.get_config().repo_bucket
        s3_client.abort_multipart_upload(Bucket=bucket_name, Key=key, UploadId=upload_id)
        logger.info(f"Aborted multipart upload (ID={upload_id}) to {bucket_name}:{key}")
        return True
    except Exception as e:
        logger.error(f"Failed to abort multipart upload: {str(e)}")
        return False


def finish_multipart_upload(key: str, upload_id: str, parts_list: List[Dict]) -> bool:
    """
    Complete a previously started multipart upload to the portal repository. See `initialize_multipart_upload`.

    Args:
        key: The destination key for the multipart upload.
        upload_id: The multipart upload ID.
        parts_list: List of completed upload parts. Each entry is a dictionary {'ETag': str, 'PartNumber': int}
            holding the upload part's entity tag (returned in response header when part is uploaded) and part number.
    Returns:
        True if successful, False otherwise. See application log for error description.
    """
    logger = app_log.get_application_logger()
    try:
        s3_client = _aws_session().client('s3')
        bucket_name = app_cfg.get_config().repo_bucket
        s3_client.complete_multipart_upload(
            Bucket=bucket_name, Key=key, MultipartUpload={'Parts': parts_list}, UploadId=upload_id)
        logger.info(f"Completed multipart upload (ID={upload_id}) to {bucket_name}:{key}")
        return True
    except Exception as e:
        logger.error(f"Failed to complete multipart upload: {str(e)}")
        return False


def _aws_session() -> Optional[Session]:
    """
    Generate an authenticated AWS session object using the authentication credentials from application configuration.

    Returns:
        The session object.
    Raises:
        Exception: If access credentials are missing from application configuration
    """
    cfg = app_cfg.get_config()
    if (not cfg.aws_access_key_id) or (not cfg.aws_access_key_secret) or (not cfg.aws_region_name):
        raise Exception("Cannot open AWS session - Missing access credentials.")
    return Session(cfg.aws_access_key_id, cfg.aws_access_key_secret, region_name=cfg.aws_region_name)


def _bucket_exists(bucket_name: str) -> bool:
    """
    Test that the specified bucket exists in the app's AWS S3 account.

    Args:
        bucket_name: The name of the bucket.
    Returns:
        True if bucket exists, else False.
    """
    try:
        session = _aws_session()
        s3_client = session.client('s3')
        response = s3_client.head_bucket(Bucket=bucket_name)
        app_log.get_application_logger().debug(f"response to head_bucket: {response}")
        return True
    except Exception as e:
        app_log.get_application_logger().warning(f"S3 bucket {bucket_name} not found: {str(e)}")
        return False


def _upload_file_to_bucket(file_path: Path, bucket_name: str, key: str, log_func: Union[bool, Callable]) -> bool:
    """
    Upload a file to the specified key in the specified bucket in AWS S3.

    Args:
        file_path: Path to file. Must exist.
        bucket_name: The name of the target S3 bucket.
        key: The key under which the file object should be stored.
        log_func: If this argument is True or is a callable function, a progress message is written to the portal app
            log to report 10% increments in progress. If the argument is a callable function, it is assumed to have the
            form `fn(pct: float)`, and the function is invoked after writing to the app log. (Of course, actual reported
            completion percentage will not necessarily be in exact 10% increments, depending on the total transfer size
            and update frequency. If the argument is False, progress is reported by overwriting a line on STDOUT each
            time the callback is invoked.
    Returns:
        True if successful; False otherwise. Error message is written to the portal application log.
    """
    xfer_cfg = TransferConfig(multipart_threshold=50*MB, multipart_chunksize=50*MB)
    logger = app_log.get_application_logger()
    try:
        session = _aws_session()
        s3_resource = session.resource('s3')
        bucket = s3_resource.Bucket(bucket_name)
        if log_func:
            logger.info(f"Starting upload: {file_path.name} to S3 bucket {bucket_name} at {key}")
        bucket.upload_file(Filename=str(file_path), Key=key,
                           Callback=_TransferProgressCallback(file_path, log_func=log_func), Config=xfer_cfg)
        if log_func:
            logger.info(f"Successfully uploaded {file_path.name} to S3.")
        return True
    except Exception:
        logger.error(f"Failed to upload file {file_path} to S3 bucket {bucket_name}", exc_info=True)
        return False


def _presigned_url_for_file(bucket_name: str, key: str, expires: int = 3600) -> Optional[str]:
    """
    Generate a presigned URL by which the specified file may be downloaded from the specified S3 bucket.

    Args:
        bucket_name: THe bucket name.
        key: The file object key.
        expires: Expiration time for the URL, in seconds. Range 1-86400 (24 hours). Default = 3600 (1 hour).
    Returns:
        The URL string, or None if an error occurred.
    """
    logger = app_log.get_application_logger()
    try:
        session = _aws_session()
        s3_client = session.client('s3')
        url = s3_client.generate_presigned_url(ClientMethod='get_object', Params={'Bucket': bucket_name, 'Key': key},
                                               ExpiresIn=expires)
        logger.info(f"Generated presigned URL for {key} in S3 bucket {bucket_name}. Expiring in {expires} seconds.")
        return url
    except Exception:
        logger.error(f"Failed to generate presigned URL for {key} in S3 bucket {bucket_name}", exc_info=True)
        return None


def _download_file_from_bucket(bucket_name: str, key: str, dst: Path, log_func: Union[bool, Callable]) -> bool:
    """
    Download a file from the specified key in the specified bucket in AWS S3.

    Args:
        bucket_name: The name of the source S3 bucket.
        key: The key under which the file object is stored within that bucket.
        dst: The file system destination path for the file object.
        log_func: If this argument is True or is a callable function, a progress message is written to the portal app
            log to report 10% increments in progress. If the argument is a callable function, it is assumed to have the
            form `fn(pct: float)`, and the function is invoked after writing to the app log. (Of course, actual reported
            completion percentage will not necessarily be in exact 10% increments, depending on the total transfer size
            and update frequency. If the argument is False, progress is reported by overwriting a line on STDOUT each
            time the callback is invoked.
    Returns:
        True if successful; False otherwise. Error message is written to the portal application log.
    """
    xfer_cfg = TransferConfig(multipart_threshold=50*MB, multipart_chunksize=50*MB)
    logger = app_log.get_application_logger()
    try:
        session = _aws_session()
        s3_resource = session.resource('s3')
        obj = s3_resource.Object(bucket_name, key)
        obj.load()
        if log_func:
            logger.info(f"Starting download from S3 bucket {bucket_name} at {key} to {dst.name}")
        s3_resource.Object(bucket_name, key).download_file(
            Filename=str(dst),
            Callback=_TransferProgressCallback(dst, log_func=log_func, download_size=obj.content_length),
            Config=xfer_cfg
        )
        if log_func:
            logger.info(f"Successfully downloaded S3 object at {key}.")
        if dst.is_file():
            return True
        else:
            logger.error(f"File downloaded from S3 successfully, but NOT found at specified destination {str(dst)}")
            return False
    except Exception:
        logger.error(f"Failed to download object {key} from S3 bucket {bucket_name}", exc_info=True)
        return False


def _file_size_in_bucket(bucket_name: str, key: str) -> int:
    """
    Return the size of the file at the specified key in the specified AWS S3 bucket.

    Args:
        bucket_name:  The name of the bucket.
        key: The file object's key.

    Returns: The file's size in bytes. Returns 0 if file not found or an internal error occurred.
    """
    try:
        session = _aws_session()
        s3_resource = session.resource('s3')
        obj_summary = s3_resource.ObjectSummary(bucket_name, key)
        obj_summary.load()
        return obj_summary.size
    except Exception:
        return 0


def _file_exists_in_bucket(bucket_name: str, key: str) -> bool:
    """
    Does a file exist at the specified key in the specified AWS S3 bucket?

    Args:
        bucket_name: The name of the bucket.
        key: The file object's key.

    Returns: True if file exists; False otherwise.
    """
    try:
        session = _aws_session()
        s3_resource = session.resource('s3')
        obj_summary = s3_resource.ObjectSummary(bucket_name, key)
        obj_summary.load()
        return True
    except Exception:
        return False


def _delete_file_in_bucket(bucket_name: str, key: str) -> bool:
    """
    Permanently delete a file object stored in an AWS S3 bucket.

    Args:
        bucket_name: The name of the S3 bucket containing the object.
        key: The object's key.
    Returns:
        True if successful or object not found; False otherwise. Error message is written to the portal application log.
    """
    logger = app_log.get_application_logger()
    if not _file_exists_in_bucket(bucket_name, key):
        logger.info(f"Attempt to delete non-existent object {key} from S3 bucket {bucket_name}")
        return True
    try:
        session = _aws_session()
        s3_resource = session.resource('s3')
        s3_resource.Object(bucket_name, key).delete()
        logger.info(f"Successfully deleted object {key} from S3 bucket {bucket_name}.")
        return True
    except Exception:
        logger.error(f"Failed to delete object {key} from S3 bucket {bucket_name}", exc_info=True)
        return False


def _delete_all_files_in_bucket(bucket_name: str, print_progress: bool = False) -> bool:
    """
    Permanently delete ALL file objects currently stored in an AWS S3 bucket.  * USE WITH CAUTION! *

    Args:
        bucket_name: The name of the S3 bucket to be emptied
        print_progress: If True, progress messages are printed to the console as each file object is removed; else,
            an INFO-level log message is written after every 10 objects are deleted. Default = False.
    Returns:
        True if successful, False otherwise.
    """
    logger = app_log.get_application_logger()
    try:
        session = _aws_session()
        s3_resource = session.resource('s3')
        bucket = s3_resource.Bucket(bucket_name)
        obj_list = [obj for obj in bucket.objects.all()]
        n = len(obj_list)
        for i, obj in enumerate(obj_list):
            s3_resource.Object(bucket_name, obj.key).delete()
            if print_progress:
                print(f"Successfully removed {obj.key} ({i} of {n} files).", file=sys.stdout, flush=True)
            elif (i > 0) and (i % 10 == 0):
                logger.info(f"Successfully removed {i} of {n} files from S3 bucket {bucket_name}")
        return True
    except Exception:
        logger.error(f"Failed to get remove all objects from S3 bucket {bucket_name}", exc_info=True)
        return False


def _bucket_contents(bucket_name: str) -> Optional[List]:
    """
    Retrieve the object listing for the specified bucket in AWS S3.

    Args:
        bucket_name: The name of the S3 bucket.

    Returns:
        A list of objects containing metadata on each object in the S3 bucket, or None if operation failed. In the
            latter case, an error message is written to the portal application log. Each element is an S3 ObjectSummary.
    """
    try:
        session = _aws_session()
        s3_resource = session.resource('s3')
        bucket = s3_resource.Bucket(bucket_name)
        obj_list = [obj for obj in bucket.objects.all()]
        return obj_list
    except Exception:
        app_log.get_application_logger().error(
            f"Failed to get object listing for S3 bucket {bucket_name}", exc_info=True)
        return None


class _TransferProgressCallback(object):
    """
    Callback object reports progress for an S3 object tranfer -- either upload or download. The callback behavior is
    configured to report progress in one of three ways:
     - Overwrite a progress message to STDOUT, which is appropriate only in the __main__ test script, when no other
       threads/processes are writing to the console.
     - Write to the portal application log each time another 10% of the transfer has completed. Of course, depending
       on how frequently the callback is invoked, the reported completion percentage will not necessarily be in exact
       10% increments.
     - If a callable is supplied of the form `progress(pct: float) -> None`, the supplied callable is invoked with the
       transfer completion percentage. This provides additional flexibility in reporting progress.
    """
    def __init__(self, file_path: Path, log_func: Union[bool, Callable], download_size: Optional[int] = None):
        """
        Initialize the S3 object transfer callback.

        Args:
            file_path: The path of file being uploaded (must exist), or the location to which file is downloaded.
            log_func: If this argument is True or is a callable function, a progress message is written to the portal
                application log to report 10% increments in progress. If the argument is a callable function, it is
                assumed to have the form `fn(pct: float)`, and the function is invoked after writing to the application
                log. (Of course, actual reported completion percentage will not necessarily be in exact 10% increments,
                depending on the total transfer size and update frequency. If the argument is False, progress is
                reported by overwriting a line on STDOUT each time the callback is invoked.
            download_size: If specified, the transfer is a download and this specifies the download file size. Else,
                the transfer is an upload and file_path must exist. Default = None.
        """
        self._path: Path = file_path
        self._log_func = log_func
        self._msg_prefix = "Downloading" if isinstance(download_size, int) else "Uploading"
        self._size = download_size if isinstance(download_size, int) else file_path.stat().st_size
        self._size_so_far = 0
        self._pct_last_update = -20
        self._lock = threading.Lock()

    def __call__(self, num_bytes):
        with self._lock:
            self._size_so_far += num_bytes
            percentage = (self._size_so_far / float(self._size)) * 100
            if self._log_func:
                if (percentage - self._pct_last_update) >= 10:
                    self._pct_last_update = percentage
                    app_log.get_application_logger().info(f"{self._msg_prefix} {self._path.name}  "
                                                          f"{self._size_so_far}/{self._size} ({percentage:.2f}%)")
                    try:
                        if callable(self._log_func):
                            self._log_func(percentage)
                    except Exception:
                        pass
            else:
                sys.stdout.write(
                    f"\r{self._msg_prefix} {self._path.name}  {self._size_so_far}/{self._size} ({percentage:.2f}%)")
                sys.stdout.flush()


def _get_lifecycle_configuration_rules_for_bucket(bucket_name: str) -> Union[List, str]:
    """
    Get the current lifecycle configuration rules for the named bucket. For administrative purposes only.

    Args:
        bucket_name: The name of the S3 bucket.
    Returns:
        List of lifecycle configuration rules (each of which is a dictionary), or an error message on failure.
    """
    try:
        session = _aws_session()
        s3_resource = session.resource('s3')
        bucket = s3_resource.Bucket(bucket_name)
        lifecycle_cfg = bucket.LifecycleConfiguration()
        lifecycle_cfg.load()
        return lifecycle_cfg.rules
    except Exception as e:
        return str(e)


def _do_multipart_upload(file_path: Path, key: str) -> Optional[str]:
    """
    Uses the Python requests library and the multipart upload support in this module to upload a file object to the
    specified key in the portal repository.

    Args:
        file_path: The file to upload.
        key: Destination key in the S3 bucket encapsulating the portal repository.
    Returns:
        None if successful, else an error description.
    """
    try:
        file_sz = file_path.stat().st_size
        ok, upload_id, chunk_size, urls = initialize_multipart_upload(file_sz, key)
        if not ok:
            return upload_id
        parts = []
        with file_path.open('rb') as f:
            sys.stdout.write("\nStarting upload...")
            for num, url in enumerate(urls):
                part = num + 1
                t = time.time()
                file_data = f.read(chunk_size)
                t_read = time.time() - t
                res = requests.put(url, data=file_data)
                t_elapsed = time.time() - t
                if res.status_code != 200:
                    aborted = abort_multipart_upload(key, upload_id)
                    return f"Error ({res.status_code}) uploading part {part}, aborted successfully={aborted}"
                etag = res.headers['ETag']
                parts.append({'ETag': etag, 'PartNumber': part})
                sys.stdout.write(
                    f"\rUploaded {part} of {len(urls)} chunks in {t_elapsed:.3f} seconds [read={t_read:.3f}]...")
                sys.stdout.flush()
            sys.stdout.write(" finishing up.\n")
        ok = finish_multipart_upload(key, upload_id, parts)
        if not ok:
            return f"Failed to finish multipart upload; be sure to remove any uploaded parts in bucket"
    except Exception as e:
        return f"Multipart upload failed: {str(e)}"


def _print_usage() -> None:
    print("\nAvailable commands:\n"
          "   b = Switch buckets.\n"
          "   l = List all file objects in bucket.\n"
          "   c = Display current lifecycle configuration for bucket.\n"
          "   u = Upload a file object to bucket.\n"
          "   d = Download a file object from bucket.\n"
          "   g = Generate a presigned URL to download a file object from bucket.\n"
          "   x = Delete a file object in bucket.\n"
          "   r = Remove ALL file objects in bucket.\n"
          "   m = Test multipart upload.\n"
          "   h = Print this usage message.\n"
          "   q = Quit.\n\n", file=sys.stdout, flush=True)


def _process_command(bucket_name: str) -> Tuple[bool, Optional[str]]:
    command = input(f"[{bucket_name}] Enter command (b, l, c, u, d, g, x, r, m, h, q) > ")
    error_msg = None
    if command == 'b':
        return False, None
    if command == 'l':
        contents = _bucket_contents(bucket_name)
        if contents is None:
            error_msg = "Unable to list bucket contents"
        elif len(contents) == 0:
            print("*** The bucket is empty! ***")
        else:
            print(f"{'KEY':50} {'STORAGE CLASS':30} {'SIZE':15} {'LAST_MODIFIED':30}")
            print(f"{'---':50} {'-------------':30} {'----':15} {'-------------':30}")
            for o in contents:
                print(f"{o.key:50} {o.storage_class:30} "
                      f"{size_with_units(o.size):15} {o.last_modified.strftime('%m-%d-%Y %H:%M:%s %Z'):30}")
    elif command == 'c':
        rules = _get_lifecycle_configuration_rules_for_bucket(bucket_name)
        if isinstance(rules, str):
            error_msg = rules
        else:
            print(f"Bucket Lifecycle Configuration for {bucket_name}:")
            print(json.dumps(rules, indent=3), file=sys.stdout, flush=True)
    elif command == 'u':
        file_path = Path(input('Enter full path to file to be uploaded > '))
        prefix = input('Enter path-like prefix, eg "/repo/folder1" (can be empty string) > ')
        if prefix is None:
            prefix = ""
        if (len(prefix) > 0) and not prefix.endswith('/'):
            prefix += "/"
        if not file_path.is_file():
            error_msg = 'Bad file path.'
        elif file_path.stat().st_size > 3*GB:
            error_msg = 'Sorry, file size must be less than 3GB'
        else:
            t_start = time.time()
            ok = _upload_file_to_bucket(file_path, bucket_name, f"{prefix}{file_path.name}", log_func=False)
            t = time.time() - t_start
            if ok:
                print(f"\nDone. {file_path.stat().st_size/MB:.1f}MB uploaded in {t:.3f} seconds.")
            else:
                error_msg = "Upload failed."
    elif command == 'd':
        dst_file = Path(input('Enter destination path (including filename) for download > '))
        obj_key = input('Enter object key in full > ')
        if dst_file.exists() or not dst_file.parent.is_dir():
            error_msg = 'Cannot overwrite existing file, or parent directory does not exist'
        else:
            t_start = time.time()
            ok = _download_file_from_bucket(bucket_name, obj_key, dst_file, log_func=False)
            t = time.time() - t_start
            if ok:
                print(f"\nDone. {dst_file.stat().st_size/MB:.1f}MB downloaded in {t:.3f} seconds.")
            else:
                error_msg = "Download failed."
    elif command == 'g':
        obj_key = input('Enter object key in full > ')
        url = _presigned_url_for_file(bucket_name, obj_key)
        if isinstance(url, str):
            print(f"\nDownload URL is: {url}")
        else:
            error_msg = "Unable to generate presigned URL; consult application logs"
    elif command == 'x':
        obj_key = input('Enter object key in full > ')
        if not _delete_file_in_bucket(bucket_name, obj_key):
            error_msg = "Delete operation failed."
    elif command == 'r':
        confirm = input('Are you sure you want to obliterate contents of the bucket? ("y" or "n") > ')
        if confirm != 'y':
            error_msg = "Operation cancelled."
        elif not _delete_all_files_in_bucket(bucket_name, print_progress=True):
            error_msg = "Delete-ALL operation failed"
    elif command == 'm':
        file_path = Path(input('Enter full path to file to be uploaded > '))
        key = input('Enter path-like key, eg "/repo/folder1/filename.ext" > ')
        if (key is None) or not key.startswith('/'):
            error_msg = 'Bad key.'
        elif not file_path.is_file():
            error_msg = 'Bad file path.'
        elif file_path.stat().st_size > 3*GB:
            error_msg = 'Sorry, file size must be less than 3GB'
        else:
            t_start = time.time()
            error_msg = _do_multipart_upload(file_path, key)
            t = time.time() - t_start
            if error_msg is None:
                print(f"\nDone. {file_path.stat().st_size/MB:.1f}MB uploaded in {t:.3f} seconds.")
    elif command == 'h':
        _print_usage()
    elif command == 'q':
        return True, None
    else:
        error_msg = f"Unrecognized command: {command}. Try again."

    print(f"ERROR: {error_msg}\n\n" if isinstance(error_msg, str) else "OK.\n\n", file=sys.stdout, flush=True)
    return False, bucket_name


# To run this module on the backend container: 'docker-compose run backend python -m database.repo
if __name__ == '__main__':
    _print_usage()

    print(f"\nCurrent working directory = {str(Path.cwd())}\n", file=sys.stdout, flush=True)

    done = False
    _bucket_name = None
    while not done:
        if _bucket_name is None:
            _bucket_name = input('Enter name of S3 bucket > ')
            if not _bucket_exists(_bucket_name):
                print(" Bucket not found! ... Exiting.\n", file=sys.stdout, flush=True)
                exit(-1)
        done, _bucket_name = _process_command(_bucket_name)

    print("\n\nBYE!", file=sys.stdout, flush=True)
