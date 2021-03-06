__FILENAME__ = megaclient
from Crypto.Cipher import AES
from Crypto.Util import Counter
from Crypto.PublicKey import RSA
from megacrypto import prepare_key, stringhash, encrypt_key, decrypt_key, enc_attr, dec_attr, aes_cbc_encrypt_a32
from megautil import a32_to_str, str_to_a32, a32_to_base64, base64_to_a32, mpi2int, base64urlencode, base64urldecode, get_chunks
import binascii
import json
import os
import random
import urllib


class MegaClient:
    def __init__(self, email, password):
        self.seqno = random.randint(0, 0xFFFFFFFF)
        self.sid = ''
        self.email = email
        self.password = password

    def api_req(self, req):
        url = 'https://g.api.mega.co.nz/cs?id=%d%s' % (self.seqno, '&sid=%s' % self.sid if self.sid else '')
        self.seqno += 1
        return json.loads(self.post(url, json.dumps([req])))[0]

    def post(self, url, data):
        return urllib.urlopen(url, data).read()

    def login(self):
        password_aes = prepare_key(str_to_a32(self.password))
        del self.password
        uh = stringhash(self.email.lower(), password_aes)
        res = self.api_req({'a': 'us', 'user': self.email, 'uh': uh})

        enc_master_key = base64_to_a32(res['k'])
        self.master_key = decrypt_key(enc_master_key, password_aes)
        if 'tsid' in res:
            tsid = base64urldecode(res['tsid'])
            if a32_to_str(encrypt_key(str_to_a32(tsid[:16]), self.master_key)) == tsid[-16:]:
                self.sid = res['tsid']
        elif 'csid' in res:
            enc_rsa_priv_key = base64_to_a32(res['privk'])
            rsa_priv_key = decrypt_key(enc_rsa_priv_key, self.master_key)

            privk = a32_to_str(rsa_priv_key)
            self.rsa_priv_key = [0, 0, 0, 0]

            for i in xrange(4):
                l = ((ord(privk[0]) * 256 + ord(privk[1]) + 7) / 8) + 2
                self.rsa_priv_key[i] = mpi2int(privk[:l])
                privk = privk[l:]

            enc_sid = mpi2int(base64urldecode(res['csid']))
            decrypter = RSA.construct((self.rsa_priv_key[0] * self.rsa_priv_key[1], 0L, self.rsa_priv_key[2], self.rsa_priv_key[0], self.rsa_priv_key[1]))
            sid = '%x' % decrypter.key._decrypt(enc_sid)
            sid = binascii.unhexlify('0' + sid if len(sid) % 2 else sid)
            self.sid = base64urlencode(sid[:43])

    def processfile(self, file):
        if file['t'] == 0 or file['t'] == 1:
            key = file['k'][file['k'].index(':') + 1:]
            key = decrypt_key(base64_to_a32(key), self.master_key)
            if file['t'] == 0:
                k = file['k'] = (key[0] ^ key[4], key[1] ^ key[5], key[2] ^ key[6], key[3] ^ key[7])
                iv = file['iv'] = key[4:6] + (0, 0)
                meta_mac = file['meta_mac'] = key[6:8]
            else:
                k = file['k'] = key
            attributes = base64urldecode(file['a'])
            attributes = dec_attr(attributes, k)
            file['a'] = attributes
        elif file['t'] == 2:
            self.root_id = file['h']
            file['a'] = {'n': 'Cloud Drive'}
        elif file['t'] == 3:
            self.inbox_id = file['h']
            file['a'] = {'n': 'Inbox'}
        elif file['t'] == 4:
            self.trashbin_id = file['h']
            file['a'] = {'n': 'Rubbish Bin'}
        return file

    def getfiles(self):
        files = self.api_req({'a': 'f', 'c': 1})
        files_dict = {}
        for file in files['f']:
            files_dict[file['h']] = self.processfile(file)
        return files_dict

    def downloadfile(self, file, dest_path):
        dl_url = self.api_req({'a': 'g', 'g': 1, 'n': file['h']})['g']

        infile = urllib.urlopen(dl_url)
        outfile = open(dest_path, 'wb')
        decryptor = AES.new(a32_to_str(file['k']), AES.MODE_CTR, counter = Counter.new(128, initial_value = ((file['iv'][0] << 32) + file['iv'][1]) << 64))

        file_mac = [0, 0, 0, 0]
        for chunk_start, chunk_size in sorted(get_chunks(file['s']).items()):
            chunk = infile.read(chunk_size)
            chunk = decryptor.decrypt(chunk)
            outfile.write(chunk)

            chunk_mac = [file['iv'][0], file['iv'][1], file['iv'][0], file['iv'][1]]
            for i in xrange(0, len(chunk), 16):
                block = chunk[i:i+16]
                if len(block) % 16:
                    block += '\0' * (16 - (len(block) % 16))
                block = str_to_a32(block)
                chunk_mac = [chunk_mac[0] ^ block[0], chunk_mac[1] ^ block[1], chunk_mac[2] ^ block[2], chunk_mac[3] ^ block[3]]
                chunk_mac = aes_cbc_encrypt_a32(chunk_mac, file['k'])

            file_mac = [file_mac[0] ^ chunk_mac[0], file_mac[1] ^ chunk_mac[1], file_mac[2] ^ chunk_mac[2], file_mac[3] ^ chunk_mac[3]]
            file_mac = aes_cbc_encrypt_a32(file_mac, file['k'])

        outfile.close()
        infile.close()

        return (file_mac[0] ^ file_mac[1], file_mac[2] ^ file_mac[3]) == file['meta_mac']

    def uploadfile(self, src_path, target, filename):
        infile = open(src_path, 'rb')
        size = os.path.getsize(src_path)
        ul_url = self.api_req({'a': 'u', 's': size})['p']

        ul_key = [random.randint(0, 0xFFFFFFFF) for _ in xrange(6)]
        encryptor = AES.new(a32_to_str(ul_key[:4]), AES.MODE_CTR, counter = Counter.new(128, initial_value = ((ul_key[4] << 32) + ul_key[5]) << 64))

        file_mac = [0, 0, 0, 0]
        for chunk_start, chunk_size in sorted(get_chunks(size).items()):
            chunk = infile.read(chunk_size)

            chunk_mac = [ul_key[4], ul_key[5], ul_key[4], ul_key[5]]
            for i in xrange(0, len(chunk), 16):
                block = chunk[i:i+16]
                if len(block) % 16:
                    block += '\0' * (16 - len(block) % 16)
                block = str_to_a32(block)
                chunk_mac = [chunk_mac[0] ^ block[0], chunk_mac[1] ^ block[1], chunk_mac[2] ^ block[2], chunk_mac[3] ^ block[3]]
                chunk_mac = aes_cbc_encrypt_a32(chunk_mac, ul_key[:4])

            file_mac = [file_mac[0] ^ chunk_mac[0], file_mac[1] ^ chunk_mac[1], file_mac[2] ^ chunk_mac[2], file_mac[3] ^ chunk_mac[3]]
            file_mac = aes_cbc_encrypt_a32(file_mac, ul_key[:4])

            chunk = encryptor.encrypt(chunk)
            outfile = urllib.urlopen(ul_url + "/" + str(chunk_start), chunk)
            completion_handle = outfile.read()
            outfile.close()

        infile.close()

        meta_mac = (file_mac[0] ^ file_mac[1], file_mac[2] ^ file_mac[3])

        attributes = {'n': filename}
        enc_attributes = enc_attr(attributes, ul_key[:4])
        key = [ul_key[0] ^ ul_key[4], ul_key[1] ^ ul_key[5], ul_key[2] ^ meta_mac[0], ul_key[3] ^ meta_mac[1], ul_key[4], ul_key[5], meta_mac[0], meta_mac[1]]
        return self.api_req({'a': 'p', 't': target, 'n': [{'h': completion_handle, 't': 0, 'a': base64urlencode(enc_attributes), 'k': a32_to_base64(encrypt_key(key, self.master_key))}]})

########NEW FILE########
__FILENAME__ = megacrypto
from Crypto.Cipher import AES
from megautil import a32_to_str, str_to_a32, a32_to_base64
import json


def aes_cbc_encrypt(data, key):
    encryptor = AES.new(key, AES.MODE_CBC, '\0' * 16)
    return encryptor.encrypt(data)


def aes_cbc_decrypt(data, key):
    decryptor = AES.new(key, AES.MODE_CBC, '\0' * 16)
    return decryptor.decrypt(data)


def aes_cbc_encrypt_a32(data, key):
    return str_to_a32(aes_cbc_encrypt(a32_to_str(data), a32_to_str(key)))


def aes_cbc_decrypt_a32(data, key):
    return str_to_a32(aes_cbc_decrypt(a32_to_str(data), a32_to_str(key)))


def stringhash(s, aeskey):
    s32 = str_to_a32(s)
    h32 = [0, 0, 0, 0]
    for i in xrange(len(s32)):
        h32[i % 4] ^= s32[i]
    for _ in xrange(0x4000):
        h32 = aes_cbc_encrypt_a32(h32, aeskey)
    return a32_to_base64((h32[0], h32[2]))


def prepare_key(a):
    pkey = [0x93C467E3, 0x7DB0C7A4, 0xD1BE3F81, 0x0152CB56]
    for _ in xrange(0x10000):
        for j in xrange(0, len(a), 4):
            key = [0, 0, 0, 0]
            for i in xrange(4):
                if i + j < len(a):
                    key[i] = a[i + j]
            pkey = aes_cbc_encrypt_a32(pkey, key)
    return pkey


def encrypt_key(a, key):
    return sum((aes_cbc_encrypt_a32(a[i:i+4], key) for i in xrange(0, len(a), 4)), ())


def decrypt_key(a, key):
    return sum((aes_cbc_decrypt_a32(a[i:i + 4], key) for i in xrange(0, len(a), 4)), ())


def enc_attr(attr, key):
    attr = 'MEGA' + json.dumps(attr)
    if len(attr) % 16:
        attr += '\0' * (16 - len(attr) % 16)
    return aes_cbc_encrypt(attr, a32_to_str(key))


def dec_attr(attr, key):
    attr = aes_cbc_decrypt(attr, a32_to_str(key)).rstrip('\0')
    return json.loads(attr[4:]) if attr[:6] == 'MEGA{"' else False

########NEW FILE########
__FILENAME__ = megafs
from megaclient import MegaClient
import errno
import fuse
import getpass
import os
import stat
import tempfile
import time

fuse.fuse_python_api = (0, 2)


class MegaFS(fuse.Fuse):
    def __init__(self, client, *args, **kw):
        fuse.Fuse.__init__(self, *args, **kw)
        self.client = client
        self.hash2path = {}
        self.files = {'/': {'t': 1, 'ts': int(time.time()), 'children': []}}

        self.client.login()
        files = self.client.getfiles()

        for file_h, file in files.items():
            path = self.getpath(files, file_h)
            dirname, basename = os.path.split(path)
            if not dirname in self.files:
                self.files[dirname] = {'children': []}
            self.files[dirname]['children'].append(basename)
            if path in self.files:
                self.files[path].update(file)
            else:
                self.files[path] = file
                if file['t'] > 0:
                    self.files[path]['children'] = []

    def getpath(self, files, hash):
        if not hash:
            return ""
        elif not hash in self.hash2path:
            path = self.getpath(files, files[hash]['p']) + "/" + files[hash]['a']['n']

            i = 1
            filename, fileext = os.path.splitext(path)
            while path in self.hash2path.values():
                path = filename + ' (%d)' % i + fileext
                i += 1

            self.hash2path[hash] = path.encode()
        return self.hash2path[hash]

    def getattr(self, path):
        if path not in self.files:
            return -errno.ENOENT

        st = fuse.Stat()
        file = self.files[path]
        st.st_atime = file['ts']
        st.st_mtime = st.st_atime
        st.st_ctime = st.st_atime
        if file['t'] == 0:
            st.st_mode = stat.S_IFREG | 0666
            st.st_nlink = 1
            st.st_size = file['s']
        else:
            st.st_mode = stat.S_IFDIR | 0755
            st.st_nlink = 2 + len([child for child in file['children'] if self.files[os.path.join(path, child)]['t'] > 0])
            st.st_size = 4096
        return st

    def readdir(self, path, offset):
        dirents = ['.', '..'] + self.files[path]['children']
        for r in dirents:
            yield fuse.Direntry(r)

    def mknod(self, path, mode, dev):
        if path in self.files:
            return -errno.EEXIST

        dirname, basename = os.path.split(path)
        self.files[dirname]['children'].append(basename)
        self.files[path] = {'t': 0, 'ts': int(time.time()), 's': 0}

    def open(self, path, flags):
        if path not in self.files:
            return -errno.ENOENT

        if (flags & 3) == os.O_RDONLY:
            (tmp_f, tmp_path) = tempfile.mkstemp(prefix='mega')
            os.close(tmp_f)
            if 'h' not in self.files[path]:
                return open(tmp_path, "rb")
            elif self.client.downloadfile(self.files[path], tmp_path):
                return open(tmp_path, "rb")
            else:
                return -errno.EACCESS
        elif (flags & 3) == os.O_WRONLY:
            if 'h' in self.files[path]:
                return -errno.EEXIST
            (tmp_f, tmp_path) = tempfile.mkstemp(prefix='mega')
            os.close(tmp_f)
            return open(tmp_path, "wb")
        else:
            return -errno.EINVAL

    def read(self, path, size, offset, fh):
        fh.seek(offset)
        return fh.read(size)

    def write(self, path, buf, offset, fh):
        fh.seek(offset)
        fh.write(buf)
        return len(buf)

    def release(self, path, flags, fh):
        fh.close()
        if fh.mode == "wb":
            dirname, basename = os.path.split(path)
            uploaded_file = self.client.uploadfile(fh.name, self.files[dirname]['h'], basename)
            if 'f' in uploaded_file:
                uploaded_file = self.client.processfile(uploaded_file['f'][0])
                self.files[path] = uploaded_file
        os.unlink(fh.name)

if __name__ == '__main__':
    email = raw_input("Email [%s]: " % getpass.getuser())
    if not email:
        email = getpass.getuser()
    password = getpass.getpass()
    client = MegaClient(email, password)
    fs = MegaFS(client)
    fs.parse(errex=1)
    fs.main()

########NEW FILE########
__FILENAME__ = megautil
import base64
import binascii
import struct


def base64urldecode(data):
    data += '=='[(2 - len(data) * 3) % 4:]
    for search, replace in (('-', '+'), ('_', '/'), (',', '')):
        data = data.replace(search, replace)
    return base64.b64decode(data)


def base64urlencode(data):
    data = base64.b64encode(data)
    for search, replace in (('+', '-'), ('/', '_'), ('=', '')):
        data = data.replace(search, replace)
    return data


def a32_to_str(a):
    return struct.pack('>%dI' % len(a), *a)


def str_to_a32(b):
    if len(b) % 4:
        b += '\0' * (4 - len(b) % 4)
    return struct.unpack('>%dI' % (len(b) / 4), b)


def a32_to_base64(a):
    return base64urlencode(a32_to_str(a))


def base64_to_a32(s):
    return str_to_a32(base64urldecode(s))


def mpi2int(s):
    return int(binascii.hexlify(s[2:]), 16)


def get_chunks(size):
    chunks = {}
    p = pp = 0
    i = 1

    while i <= 8 and p < size - i * 0x20000:
      chunks[p] = i * 0x20000
      pp = p
      p += chunks[p]
      i += 1

    while p < size:
      chunks[p] = 0x100000
      pp = p
      p += chunks[p]

    chunks[pp] = size - pp
    if not chunks[pp]:
      del chunks[pp]

    return chunks

########NEW FILE########
