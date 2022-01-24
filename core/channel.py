import logging
from pathlib import Path
import shutil
import hashlib
import base64
import tarfile
import tempfile
from itertools import groupby
import datetime

from core.exceptions import MicrodotError
from core import state
from core import CONFLICT_EXT, ENCRYPTED_DIR_EXT, ENCRYPTED_FILE_EXT, ENCRYPTED_DIR_FORMAT, ENCRYPTED_FILE_FORMAT
from core import CONFLICT_FILE_EXT, CONFLICT_DIR_EXT, TIMESTAMP_FORMAT, DECRYPTED_DIR, SCAN_CHANNEL_BLACKLIST, SCAN_DIR_BLACKLIST

from core.utils import confirm, colorize, debug, info, get_hash, get_tar
from core.utils import Columnize

try:
    from cryptography.fernet import Fernet
    import cryptography
except ImportError as e:
    print(f"ImportError: {e}")
    sys.exit(1)

logger = logging.getLogger("microdot")


"""
    You can add a new encrypted file with: $ md --init file.txt -e
    This will:
        - Move the file to the channel directory
        - Encrypt the file, using the extension: .encrypted
        - Decrypt the encrypted file and place it next to the encrypted file.
        - Add the non-encrypted file to the .gitignore file to protect it from pushing to GIT.
        
    When linking an encrypted file:
        The encrypted file will be visible in the list without the .encrypted extension but with a [E] marker
        The encrypted file can be linked as normal with: $ md --link file.txt
        This will:
            - Decrypt the corresponding encrypted file and place it next to the encrypted file.
            - Add the non-encrypted file to the .gitignore file to protect it from pushing to GIT.

    When unlinking an encrypted file:
        The encrypted file will be visible in the list without the .encrypted extension but with a [E] marker
        The encrypted file can be unlinked as normal with: $ md --unlink file.txt
        This will:
            - Remove the link
            - Remove the un-encrypte file
            - Remove the file entry on the .gitignore file

    When the repository is updated, the linked encrypted files need to be decrypted by using: $ md --update
    We can automate this by managing the GIT repo for the user, but this will add more complexity.
"""

class DotFileBaseClass():
    def __init__(self, path, channel):
        """ path is where dotfile source is: /home/eco/.dotfiles/common/testfile.txt """

        self.channel = channel
        self.path = path
        self.name = path.relative_to(channel)
        self.link_path = Path.home() / self.name
        self.is_encrypted = False
        self.cleanup_link()

    def cleanup_link(self):
        # find orphan links (symlink that points 
        if self.link_path.is_symlink():
            if not self.path.exists():
                self.link_path.unlink()
                info("link_check", "remove", f"orphan link found: {self.link_path}")
            elif not self.link_path.resolve() == self.path:
                info("link_check", "remove", f"link doesn't point to data: {self.link_path}")
                self.link_path.unlink()

    def check_symlink(self):
        # check if link links to src
        if not self.link_path.is_symlink():
            return
        return self.link_path.resolve() == self.path

    def is_dir(self):
        return self.path.is_dir()

    def is_file(self):
        return self.path.is_file()

    def link(self, target=None, force=False):
        link = self.link_path

        if not target:
            target = self.path

        if link.is_symlink():
            link.unlink()

        if link.exists() and force:
            logger.info(f"Link path exists, using --force to overwrite: {link}")
            self.remove_path(link)

        if link.is_symlink():
            raise MicrodotError(f"Dotfile already linked: {link}")

        if link.exists():
            raise MicrodotError(f"Link exists: {link}")

        link.symlink_to(target)
        debug(self.name, 'linked', f'{link} -> {target.name}')
        return True
    
    def unlink(self):
        if not self.check_symlink():
            logger.error(f"Dotfile is not linked: {self.name}")
            return

        self.link_path.unlink()
        debug(self.name, 'unlinked', self.link_path)
        return True

    def init(self, src):
        """ Move source path to dotfile location """
        src.replace(self.path)
        debug(self.name, 'moved', f'{src} -> {self.path}')
        self.link()

    def remove_path(self, path: Path):
        """ Remove file or directory """
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=False, onerror=None)
        else:
            path.unlink()

    def do_encrypt(self, key):
        """ Encrypt an unencrypted dotfile """
        if (was_linked := self.check_symlink()):
            self.unlink()

        if self.path.is_dir():
            df = DotDirEncrypted(self.path, self.channel, key)
        else:
            df = DotFileEncrypted(self.path, self.channel, key)
        

class DotFileEncryptedBaseClass(DotFileBaseClass):
    """ Baseclass for all encrypted files/directories """
    def __init__(self, path, channel, key):
        try: # parse ENCRYPTED file: ~/.dotfiles/common/testdir#IzjOuV4h#20220121162145#D#CRYPT
            name, self.hash, ts,  _, _ = path.name.split('#')
            self.path = channel.parent / DECRYPTED_DIR / channel.name / path.relative_to(channel).parent / name
            self.encrypted_path = path
            self.name = self.path.relative_to(channel.parent / DECRYPTED_DIR / channel.name)
            self.timestamp = datetime.datetime.strptime(ts, TIMESTAMP_FORMAT)
        except ValueError:
            try: # parse CONFLICT file: ~/.dotfiles/common/testdir#IzjOuV4h#20220121162145#D#CRYPT
                name, self.hash, ts,  _, _, _ = path.name.split('#')
                self.path = channel.parent / DECRYPTED_DIR / channel.name / path.relative_to(channel).parent / name
                self.encrypted_path = path
                self.name = self.path.relative_to(channel.parent / DECRYPTED_DIR / channel.name)
                self.timestamp = datetime.datetime.strptime(ts, TIMESTAMP_FORMAT)
            except ValueError:
                try: # parse path that will be used by init to initiate new encrypted dotfile: ~/.dotfiles/common/testfile.txt
                     # allow incomplete data. missing data will be added later
                    self.hash = None
                    self.path = channel.parent / DECRYPTED_DIR / channel.name / path.relative_to(channel)
                    self.name = path.relative_to(channel)
                    self.encrypted_path = self.get_encrypted_path(channel, self.name)
                    self.timestamp = datetime.datetime.utcnow()
                except ValueError:
                    raise MicrodotError(f"Failed to parse path: {path}")

        self.channel = channel
        self.link_path = Path.home() / self.name
        self.is_encrypted = True
        self._key = key

        # cleanup orphan links (symlink that points 
        self.cleanup_link()

        # ensure decrypted dir exists
        if not self.path.parent.is_dir():
            debug(self.name, 'mkdir', self.path.parent)
            self.path.parent.mkdir(parents=True)

    def encrypt(self, src, key=None, force=False):
        """ Do some encryption here and write to self.encrypted_path """
        # TODO encrypt should decide on encrypted_path here because it depends on the given src

        if key == None:
            key = self._key

        self.encrypted_path = self.get_encrypted_path(self.channel, self.name, src=src)

        # if dir, compress dir into tmp tar file
        if src.is_dir():
            src = get_tar(src)

        if self.encrypted_path.exists():
            if force:
                self.remove_path(self.encrypted_path)
            else:
                raise MicrodotError(f"Encrypted file exists in channel: {self.encrypted_path}")

        fernet = Fernet(key)
        encrypted = fernet.encrypt(src.read_bytes())

        # cleanyp tmp file
        if src.is_dir():
            src.unlink()

        self.encrypted_path.write_bytes(encrypted)
        debug(self.name, 'encrypted', f'{src.name} -> {self.encrypted_path}')

    def decrypt(self, dest=None):
        """ Do some decryption here and write to dest path """
        if dest == None:
            dest = self.path

        if dest.exists():
            dest.unlink()

        try:
            fernet = Fernet(self._key)
            decrypted = fernet.decrypt(self.encrypted_path.read_bytes())
        except cryptography.fernet.InvalidToken:
            raise MicrodotError(f"Failed to decrypt {self.encrypted_path}, invalid key.")

        dest.write_bytes(decrypted)
        debug(self.name, 'decrypted', f'{self.encrypted_path.name} -> {dest}')

    def link(self, force=False):
        self.decrypt()
        DotFileBaseClass.link(self, force=force)

    def update(self):
        """ Update encrypted file if decrypted file/dir has changed from encrypted file """
        if not self.check_symlink():
            logger.error(f"Dotfile not linked {self.name}")
            return
        if not self.is_changed():
            return
        info(self.name, 'changed', self.path)

        old_encrypted_path = self.encrypted_path
        self.encrypt(self.path, self._key, force=True)
        self.unlink()
        old_encrypted_path.unlink()
        self.link()

        info(self.name, 'updated', f'{self.name} -> {self.encrypted_path.name}')

    def is_changed(self):
        """ Indicate if decrypted dir has changed from encrypted file
            Checks current file md5 against last md5 """
        return not self.check_symlink() or self.hash != get_hash(self.path)

    def get_encrypted_path(self, channel, name, src=None):
        """ If src is specified, calculate hash from this source instead of standard decrypted data location """
        if src == None:
            md5 = get_hash(Path.home() / name)
        else:
            md5 = get_hash(src)

        ts = datetime.datetime.utcnow().strftime(TIMESTAMP_FORMAT)
        if self.is_dir():
            return channel / ENCRYPTED_DIR_FORMAT.format(name=name, ts=ts, md5=md5)
        else:
            return channel / ENCRYPTED_FILE_FORMAT.format(name=name, ts=ts, md5=md5)

    def unlink(self):
        if not DotFileBaseClass.unlink(self):
            return
        self.remove_path(self.path)
        debug(self.name, 'removed', f'decrypted path: {self.path.name}')

    def init(self, src):
        """ Move source path to dotfile location """
        self.encrypt(src, self._key)
        self.remove_path(src)
        debug(self.name, 'init', f'removed original path: {src}')
        self.link()


class DotFileEncrypted(DotFileEncryptedBaseClass):
    def __init__(self, *args):
        super().__init__(*args)

    def is_file(self):
        return True

    def is_dir(self):
        return False


class DotDirEncrypted(DotFileEncryptedBaseClass):
    def __init__(self, *args):
        super().__init__(*args)

    def is_file(self):
        return False

    def is_dir(self):
        return True

    def decrypt(self, dest=None):
        if dest == None:
            dest = self.path

        tmp_dir = Path(tempfile.mkdtemp())
        tmp_file = Path(tempfile.mktemp())

        DotFileEncryptedBaseClass.decrypt(self, tmp_file)

        with tarfile.open(tmp_file, 'r') as tar:
            tar.extractall(tmp_dir)

        if dest.exists():
            shutil.rmtree(dest, ignore_errors=False, onerror=None)

        # cant use pathlib's replace because files need to be on same filesystem
        shutil.move((tmp_dir / self.name), dest)
        debug(self.name, "moved", f"{tmp_dir/self.name} -> {dest}")

        tmp_file.unlink()


class Channel():
    """ Represents a channel, holds encrypted and unencrypted dotfiles. """
    def __init__(self, path, state):
        self._key = state.encryption.key
        self._path = path
        self.name = path.name
        self.dotfiles = self.search_dotfiles(self._path, state.core.check_dirs)
        self.dotfiles = self.filter_decrypted(self.dotfiles)
        self._colors = state.colors
        self.conflicts = sorted(self.search_conflicts(self._path, state.core.check_dirs), key=lambda x: x.timestamp, reverse=True)

    def create_obj(self, path):
        """ Create a brand new DotFileBaseClass object """
        if path.name.endswith(ENCRYPTED_DIR_EXT) or path.name.endswith(CONFLICT_DIR_EXT):
            return DotDirEncrypted(path, self._path, self._key)
        elif path.name.endswith(ENCRYPTED_FILE_EXT) or path.name.endswith(CONFLICT_FILE_EXT):
            return DotFileEncrypted(path, self._path, self._key)
        return DotFileBaseClass(path, self._path)

    def filter_decrypted(self, dotfiles):
        """ Check if there are decrypted paths in the list """
        ret = [df for df in dotfiles if df.is_encrypted]
        encr_paths = [df.path for df in dotfiles if df.is_encrypted]

        for df in dotfiles:
            if df.path not in encr_paths:
                ret.append(df)
        return ret

    def search_dotfiles(self, item, search_dirs):
        """ recursive find of files and dirs in channel when file/dir is in search_dirs """
        items = [self.create_obj(f) for f in item.iterdir() if f.is_file() and not f.name.endswith(CONFLICT_EXT)]

        for d in [d for d in item.iterdir() if d.is_dir()]:
            if d.name in SCAN_DIR_BLACKLIST:
                continue
            if d.name in search_dirs:
                items += self.search_dotfiles(d, search_dirs)
            else:
                items.append(self.create_obj(d))
        return sorted(items, key=lambda item: item.name)

    def search_conflicts(self, item, search_dirs):
        """ recursive find of files and dirs in channel when file/dir is in search_dirs """
        items = [self.create_obj(f) for f in item.iterdir() if f.is_file() and f.name.endswith(CONFLICT_EXT)]

        for d in [d for d in item.iterdir() if d.is_dir()]:
            if d.name in SCAN_DIR_BLACKLIST:
                continue
            if d.name not in search_dirs:
                items.append(self.create_obj(d))
        return sorted(items, key=lambda item: item.name)

    def list(self):
        """ Pretty print all dotfiles """
        print(colorize(f"\nchannel: {self.name}", self._colors.channel_name))

        encrypted =  [d for d in self.dotfiles if d.is_dir() and d.is_encrypted]
        encrypted += [f for f in self.dotfiles if f.is_file() and f.is_encrypted]
        items =  [d for d in self.dotfiles if d.is_dir() and not d.is_encrypted]
        items += [f for f in self.dotfiles if f.is_file() and not f.is_encrypted]

        if len(items) == 0 and len(encrypted) == 0:
            print(colorize(f"No dotfiles yet!", 'red'))
            return

        cols = Columnize(tree=True, prefix_color='magenta')

        for item in items:
            color = self._colors.linked if item.check_symlink() else self._colors.unlinked

            if item.is_dir():
                cols.add([colorize(f"[D]", color), item.name])
            else:
                cols.add([colorize(f"[F]", color), item.name])

        for item in encrypted:
            color = self._colors.linked if item.check_symlink() else self._colors.unlinked
            if item.is_dir():
                cols.add([colorize(f"[ED]", color),
                          item.name,
                          colorize(item.hash, 'green'),
                          colorize(f"{item.timestamp}", 'magenta')])
            else:
                cols.add([colorize(f"[EF]", color),
                          item.name,
                          colorize(item.hash, 'green'),
                          colorize(f"{item.timestamp}", 'magenta')])
        cols.show()

        #cols = Columnize()
        cols = Columnize(prefix='  ', prefix_color='red')
        for item in self.conflicts:
            if item.is_dir():
                cols.add([colorize(f"[CD]", self._colors.conflict),
                          colorize(f"{item.timestamp}", 'magenta'),
                          colorize(f"{item.encrypted_path.name}", "green")])
            else:
                cols.add([colorize(f"[CF]", self._colors.conflict),
                          colorize(f"{item.timestamp}", 'magenta'),
                          colorize(f"{item.encrypted_path.name}", "green")])
        cols.show()

    def get_dotfile(self, name):
        """ Get dotfile object by filename """
        for df in self.dotfiles:
            if str(df.name) == str(name):
                return df

    def get_conflict(self, name):
        """ Get DotFile object by conflict file name """
        for df in self.conflicts:
            if str(df.encrypted_path.name) == str(name):
                return df

    def link_all(self, force=False, assume_yes=False):
        """ Link all dotfiles in channel """
        dotfiles = [df for df in self.dotfiles if not df.check_symlink()]
        for df in dotfiles:
            info("link_all", "list", df.name)
        if confirm(f"Link all dotfiles in channel {self.name}?", assume_yes):
            for dotfile in self.dotfiles:
                dotfile.link(force=force)

    def unlink_all(self, assume_yes=False):
        """ Unlink all dotfiles in channel """
        dotfiles = [df for df in self.dotfiles if df.check_symlink()]
        for df in dotfiles:
            info("unlink_all", "list", df.name)
        if confirm(f"Unlink all dotfiles in channel {self.name}?", assume_yes):
            for dotfile in dotfiles:
                dotfile.unlink()

    def init(self, path, encrypted=False):
        """ Start using a dotfile.
            Copy dotfile to channel directory and create symlink. """

        src = self._path / path.absolute().relative_to(Path.home())

        if encrypted:
            if path.is_file():
                dotfile = DotFileEncrypted(src, self._path, self._key)
            elif path.is_dir():
                dotfile = DotDirEncrypted(src, self._path, self._key)
            else:
                raise MicrodotError(f"Don't know what to do with this path: {path}")
        else:
            dotfile = DotFileBaseClass(src, self._path)

        #dotfile = self.create_obj(src)

        if self.get_dotfile(dotfile.name):
            logger.error(f"Dotfile already exists in channel: {dotfile.name}")
            return

        if not (path.is_file() or path.is_dir()):
            logger.error(f"Source path is not a file or directory: {path}")
            return

        if path.is_symlink():
            logger.error(f"Source path is a symlink: {path}")
            return

        dotfile.init(path)
        return dotfile


def get_channels(state):
    """ Find all channels in dotfiles dir and create Channel objects """
    path      = state.core.dotfiles_dir
    blacklist = state.core.channel_blacklist + SCAN_CHANNEL_BLACKLIST
    return [ Channel(d, state) for d in Path(path).iterdir() if d.is_dir() and d.name not in blacklist ]

def get_channel(name, state, create=False, assume_yes=False):
    """ Find or create and return Channel object """
    name         = name if name else "common"
    dotfiles_dir = state.core.dotfiles_dir
    path         = dotfiles_dir / name

    if not path.is_dir():
        if not create:
            raise MicrodotError(f"Channel {name} not found")

        if not confirm(f"Channel {name} doesn't exist, would you like to create it?", assume_yes=assume_yes):
            return
        try:
            path.mkdir(parents=True)
            logger.info(f"Created channel: {name}")
        except PermissionError as e:
            logger.error(f"Failed to create channel: {name}")
            raise MicrodotError("Failed to create channel: {name}")

    for channel in get_channels(state):
        if channel.name == name:
            return channel

    raise MicrodotError(f"This should be unreachable, failed to find channel: {name}")

# TODO below should be part of channel class??
def get_encrypted_dotfiles(linked=False, grouped=False):
    """ Return encrypted dotfiles
        grouped=True: doubles are grouped by filename, will be used to find conflicting files
        linked=True:  only return dotfiles that are linked """

    items = []
    keyfunc = lambda x: x.name

    for channel in get_channels(state):
        data = [x for x in channel.dotfiles if x.is_encrypted]

        if linked:
            data = [x for x in data if x.check_symlink()]

        data = sorted(data, key=keyfunc)

        if grouped:
            for k, g in groupby(data, keyfunc):
                items.append(list(g))
        else:
            items += data

    return items

def update_encrypted_from_decrypted():
    for df in get_encrypted_dotfiles(linked=True):
        df.update()

def update_decrypted_from_encrypted(paths):
    """ Redecrypt all encrypted dotfiles on update """
    # TODO This is the only place in the code where state is not explicitly passed
    #      It isn't pretty but this funciton is used as a callback, so yeah... needs fixin!
    for p in paths:
        channel = get_channel(p.parts[0], state, create=False)
        df_path = p.relative_to(channel.name). with_suffix('')
        dotfile = channel.get_dotfile(df_path)
        dotfile.decrypt()
