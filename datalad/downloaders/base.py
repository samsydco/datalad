# emacs: -*- mode: python; py-indent-offset: 4; tab-width: 4; indent-tabs-mode: nil -*-
# ex: set sts=4 ts=4 sw=4 noet:
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
#
#   See COPYING file distributed along with the datalad package for the
#   copyright and license terms.
#
# ## ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ### ##
"""Provide access to stuff (html, data files) via HTTP and HTTPS

"""

__docformat__ = 'restructuredtext'

import os
from os.path import exists, join as opj, isdir

from ..ui import ui
from ..utils import auto_repr

from logging import getLogger
lgr = getLogger('datalad.downloaders')

@auto_repr
class BaseDownloader(object):
    """Base class for the downloaders"""

    _DEFAULT_AUTHENTICATOR = None

    def __init__(self, credential=None, authenticator=None):
        """

        Parameters
        ----------
        credential: Credential, optional
          Provides necessary credential fields to be used by authenticator
        authenticator: Authenticator, optional
          Authenticator to use for authentication.
        """
        self.credential = credential
        if not authenticator and self._DEFAULT_AUTHENTICATOR:
            authenticator = self._DEFAULT_AUTHENTICATOR()

        if authenticator:
            if not credential:
                raise ValueError(
                    "Both authenticator and credentials must be provided."
                    " Got only authenticator %s" % repr(authenticator))

        self.authenticator = authenticator


    def _access(self, method, url, allow_old_session=True, **kwargs):
        """Fetch content as pointed by the URL optionally into a file

        Parameters
        ----------
        method : callable
          A callable, usually a method of the same class, which we decorate
          with access handling, and pass url as the first argument
        url : string
          URL to access
        *args, **kwargs
          Passed into the method call

        Returns
        -------
        None or bytes
        """
        # TODO: possibly wrap this logic outside within a decorator, which
        # would just call the corresponding method

        authenticator = self.authenticator
        needs_authentication = authenticator and authenticator.requires_authentication

        attempt = 0
        while True:
            attempt += 1
            if attempt > 20:
                # are we stuck in a loop somehow? I think logic doesn't allow this atm
                raise RuntimeError("Got to the %d'th iteration while trying to download %s" % (attempt, url))

            try:
                used_old_session = False
                access_denied = False
                used_old_session = self._establish_session(url, allow_old=allow_old_session)
                if not allow_old_session:
                    assert(not used_old_session)
                lgr.log(5, "Calling out into %s for %s" % (method, url))
                result = method(url, **kwargs)
                # assume success if no puke etc
                break
            except AccessDeniedError as e:
                lgr.debug("Access was denied: %s", e)
                access_denied = True
            except DownloadError:
                # TODO Handle some known ones, possibly allow for a few retries, otherwise just let it go!
                raise

            if access_denied:  # moved logic outside of except for clarity
                if needs_authentication:
                    # so we knew it needs authentication
                    if used_old_session:
                        # Let's try with fresh ones
                        allow_old_session = False
                        continue
                    else:
                        # we did use new cookies, we knew that authentication is needed
                        # but still failed. So possible cases:
                        #  1. authentication credentials changed/were revoked
                        #     - allow user to re-enter credentials
                        #  2. authentication mechanisms changed
                        #     - we can't do anything here about that
                        #  3. bug in out code which would render authentication/cookie handling
                        #     ineffective
                        #     - not sure what to do about it
                        if ui.yesno(
                                title="Authentication to access {url} has failed".format(url=url),
                                text="Do you want to enter other credentials in case they were updated?"):
                            self.credential.enter_new()
                            allow_old_session = False
                            continue
                        else:
                            raise DownloadError("Failed to download from %s given available credentials" % url)
                else:  # None or False
                    if needs_authentication is False:
                        # those urls must or should NOT require authentication but we got denied
                        raise DownloadError("Failed to download from %s, which must be available without "
                                            "authentication but access was denied" % url)
                    else:
                        assert(needs_authentication is None)
                        # So we didn't know if authentication necessary, and it seems to be necessary, so
                        # Let's ask the user to setup authentication mechanism for this website
                        raise AccessDeniedError(
                            "Access to %s was denied but we don't know about this data provider. "
                            "You would need to configure data provider authentication using TODO " % url)

        return result

    @staticmethod
    def _get_temp_download_filename(filepath):
        """Given a filepath, return the one to use as temp file during download
        """
        # TODO: might better reside somewhere under .datalad/tmp or .git/datalad/tmp
        return filepath + ".datalad-download-temp"


    def _get_download_details(self, url):
        """

        Parameters
        ----------
        url : str

        Returns
        -------
        downloader_into_fp: callable
           Which takes two parameters: file, pbar
        target_size: int or None (if uknown)
        url_filename: str or None
           Filename as decided from the url
        """
        raise NotImplementedError("Must be implemented in the subclass")

    def _download(self, url, path=None, overwrite=False):
        """

        Parameters
        ----------
        url: str
          URL to download
        path: str, optional
          Path to file where to store the downloaded content.  If None, downloaded
          content provided back in the return value (not decoded???)

        Returns
        -------
        None or bytes

        """

        downloader, target_size, url_filename = self._get_download_details(url)

        #### Specific to download
        if path:
            if isdir(path):
                # provided path is a directory under which to save
                filename = url_filename
                filepath = opj(path, filename)
            else:
                filepath = path
        else:
            filepath = url_filename

        if exists(filepath) and not overwrite:
            raise DownloadError("File %s already exists" % filepath)

        # FETCH CONTENT
        # TODO: pbar = ui.get_progressbar(size=response.headers['size'])
        # TODO: logic to fetch into a nearby temp file, move into target
        #     reason: detect aborted downloads etc
        try:
            temp_filepath = self._get_temp_download_filename(filepath)
            if exists(temp_filepath):
                # eventually we might want to continue the download
                lgr.warning(
                    "Temporary file %s from the previous download was found. "
                    "It will be overriden" % temp_filepath)
                # TODO.  also logic below would clean it up atm

            with open(temp_filepath, 'wb') as fp:
                # TODO: url might be a bit too long for the beast.
                # Consider to improve to make it animated as well, or shorten here
                pbar = ui.get_progressbar(label=url, fill_text=filepath, maxval=target_size)
                downloader(fp, pbar)
                pbar.finish()
            downloaded_size = os.stat(temp_filepath).st_size

            # (headers.get('Content-type', "") and headers.get('Content-Type')).startswith('text/html')
            #  and self.authenticator.html_form_failure_re: # TODO: use information in authenticator
            if self.authenticator and downloaded_size < 10000 \
                    and (hasattr(self.authenticator, 'failure_re') and self.authenticator.failure_re):
                # TODO: Common logic for any downloader whenever no proper failure code is thrown etc
                with open(temp_filepath) as fp:
                    assert hasattr(self.authenticator, 'check_for_auth_failure'), \
                        "%s has failure_re defined but no check_for_auth_failure" \
                        % self.authenticator
                    self.authenticator.check_for_auth_failure(
                        fp.read(), "Download of file %s has failed: " % filepath)

            if target_size and target_size != downloaded_size:
                lgr.error("Downloaded file size %d differs from originally announced %d",
                          downloaded_size, target_size)
                raise DownloadError("Downloaded size %d differs from original %d" % (downloaded_size, target_size))

            # place successfully downloaded over the filepath
            os.rename(temp_filepath, filepath)

            # TODO: adjust ctime/mtime according to headers
            # TODO: not hardcoded size, and probably we should check header

        except AccessDeniedError as e:
            raise
        except Exception as e:
            lgr.error("Failed to download {url} into {filepath}: {e}".format(
                **locals()
            ))
            raise DownloadError  # for now
        finally:
            if exists(temp_filepath):
                # clean up
                lgr.debug("Removing a temporary download %s", temp_filepath)
                os.unlink(temp_filepath)

        return filepath

    def download(self, url, path=None, **kwargs):
        """Fetch content as pointed by the URL optionally into a file

        Parameters
        ----------
        url : string
          URL to access
        path : str, optional
          Either full path to the file, or if exists and a directory
          -- the directory to save under. If just a filename -- store
          under curdir. If None -- fetch and return the fetched content.

        Returns
        -------
        None or bytes
        """
        # TODO: may be move all the path dealing logic here
        # but then it might require sending request anyways for Content-Disposition
        # so probably nah
        lgr.info("Downloading %r into %r", url, path)
        return self._access(self._download, url, path=path, **kwargs)

    def check(self, url, **kwargs):
        """
        Parameters
        ----------
        url : string
          URL to access
        """
        return self._access(self._check, url, **kwargs)


# Exceptions.  might migrate elsewhere

class DownloadError(Exception):
    pass

class AccessDeniedError(DownloadError):
    pass

#
# Authenticators    XXX might go into authenticators.py
#

class Authenticator(object):
    """Abstract common class for different types of authentication

    Derived classes should get parameterized with options from the config files
    from "provider:" sections
    """
    requires_authentication = True
    # TODO: figure out interface

    def authenticate(self, *args, **kwargs):
        """Derived classes will provide specific implementation
        """
        if self.requires_authentication:
            raise NotImplementedError("Authentication for %s not yet implemented" % self.__class__)

class NotImplementedAuthenticator(Authenticator):
    pass

class NoneAuthenticator(Authenticator):
    """Whenever no authentication is necessary and that is stated explicitly"""
    requires_authentication = False
    pass

