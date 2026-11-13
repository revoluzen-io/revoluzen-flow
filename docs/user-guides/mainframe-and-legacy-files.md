# Mainframe and older files

Flow reads the files that mainframes and older business systems send, without any code: fixed-width text, EBCDIC text, COBOL copybook records with packed decimals, and PGP-encrypted files. It can also encrypt the files it writes for a partner.

Everything here is set in the **Source** step (or **Destination**): under **Read the file as**, choose **Any format**. That opens the File settings, where **Format Override** offers **Fixed-width text** and **COBOL copybook records (mainframe)**, **Encoding** offers the EBCDIC code pages, and **Decrypt with PGP** sits at the bottom.

## Before you start: how the file left the mainframe

How a file was copied off the mainframe decides how Flow must read it.

| The file was copied | What it looks like | Choose in Flow |
|---|---|---|
| In text (ASCII) mode | Readable text, one record per line | **Records: One record per line**, code page **Latin-1** |
| In binary mode from a fixed-length dataset (RECFM=F or FB) | Unreadable in a text editor, no line breaks | **Records: Fixed length**, an **EBCDIC** code page |
| In binary mode with the record lengths kept (RECFM=V or VB, for example FTP `quote site rdw`) | As above, records of different lengths | **Records: Variable length (RDW)**, or **in blocks (BDW and RDW)** if the blocks were kept too |

A file with packed (COMP-3) or binary (COMP) numbers must be copied in **binary**. A text-mode copy changes those bytes and the numbers cannot be recovered.

## Fixed-width text

Each column sits at the same position on every line.

1. Set **Format Override** to **Fixed-width text**.
2. Add a row per column under **Fixed-width columns**: its name, **Start** (the first position is 1), **Length** and type.
   If you have a list of widths instead, type them in **Or make the columns from a list of widths** (for example `8, 30, 12`), add names if you have them, and click **Make columns**. The rows fill in and you can then set each type.
3. Choose the **Encoding**: UTF-8 or Latin-1 for ordinary text, an EBCDIC code page for mainframe text.

| Type | Reads | Example |
|---|---|---|
| Text | The characters as they are | `ACME LTD` |
| Whole number | Digits with an optional sign | `00042` = 42 |
| Number with a decimal point | A written point, thousands separators, a sign in front or behind, `CR` or `DB` | `1,234.50-` = -1234.50 |
| Number with implied decimals | Digits only, with the decimal places implied. A sign carried on the last digit (as mainframe text does) is read | `0000012345` with 2 places = 123.45; `000000050J` = -5.01 |
| Date | A format such as `YYYYMMDD`, `DD/MM/YYYY` or `YYYYDDD` | `20261101` |

Other settings:

- **Skip lines at start / at end** drop header and trailer records, in every file.
- **Decimal point** chooses between `1,234.50` and `1.234,50`.
- **Spaces in text** removes spaces on both sides, the right only, the left only, or keeps them.
- **Empty text becomes NULL** is on by default.
- **When a value cannot be read**: **Stop the run and say where** names the line, the column and what was found. **Leave that value empty and go on** sets that one value to NULL.

Positions count characters. In EBCDIC, Latin-1 and Windows-1252 one character is one byte.

## EBCDIC text

EBCDIC is the character set of IBM mainframes and IBM i. Choose the code page in **Encoding** (for CSV and fixed-width text) or in **Code page** (for copybook records):

| Code page | Used in |
|---|---|
| EBCDIC 037 | US, Canada, and most English-language installations |
| EBCDIC 1140 | The same, with the euro sign |
| EBCDIC 1047 | z/OS UNIX and C programs |
| EBCDIC 500 | International (Belgium, Switzerland) |
| EBCDIC 273 | Germany and Austria |
| EBCDIC 875, 1026, 424 | Greek, Turkish, Hebrew |

If the brackets or the `^` come out wrong, try 1047 instead of 037: those are the characters where the two differ. A CSV file in EBCDIC is read the same way: choose the code page in **Encoding**. The **Encoding** setting now applies to CSV files in Latin-1, Windows-1252 and UTF-16 too.

## COBOL copybook records

A copybook is the COBOL description of a record. Flow turns it into typed columns.

1. Set **Format Override** to **COBOL copybook records (mainframe)**.
2. Paste the copybook into **Copybook**: the 01 level and every field under it. Sequence numbers in columns 1 to 6 are fine; columns 73 to 80 are ignored, as in COBOL. Or give the path of a `.cpy` file in **Or: copybook file**.
3. Click **Check copybook**. Flow lists every column with its start position, its length in bytes and how it will be read, and the record length. A mistake is shown with its line number.
4. Choose the **Code page** and the **Records** setting that match how the file was copied (see the first section).

What Flow reads:

| In the copybook | You get |
|---|---|
| `PIC X(n)`, `PIC A(n)` | Text, with trailing spaces removed (change it in **Spaces in text**) |
| `PIC 9(n)`, `PIC S9(n)` | A whole number. The sign is read from the last digit, or from `SIGN LEADING` / `SEPARATE` |
| `V` in the picture, such as `PIC S9(7)V99` | A decimal number with that many places, never rounded |
| `COMP-3` or `PACKED-DECIMAL` | Packed decimal, two digits per byte, sign in the last half-byte |
| `COMP`, `COMP-4`, `COMP-5`, `BINARY` | Binary numbers. Mainframe order by default; **Binary numbers: PC** for COMP-5 written on a PC |
| `COMP-1`, `COMP-2` | Floating point, IBM format by default; **IEEE** for files from a PC |
| Edited pictures such as `ZZ,ZZ9.99-` | A decimal number |
| `PIC N(n)` | National (UTF-16) text |
| `OCCURS n TIMES` | One column per item: `MONTHLY_SALES_1`, `MONTHLY_SALES_2`, ... |
| `OCCURS m TO n TIMES DEPENDING ON field` | Columns up to the most items; items past the count are empty, and the fields after the table move up |
| `REDEFINES` | Every part is read, unless a rule says which one a record uses (below) |
| Several 01 records | Treated as different layouts of the same record, chosen by rules |
| `88` levels, `VALUE`, `66 RENAMES`, `JUSTIFIED`, `BLANK WHEN ZERO`, `INDEXED BY` | Read past |
| `FILLER` | Skipped; its bytes are kept in the layout |

A number that is all spaces or all binary zeros reads as empty. Column names use underscores (`CUST_ID`) so SQL steps can use them without quotes; **Column names: as in the copybook** keeps `CUST-ID`. Two fields with the same name in different groups get the group name in front (`BUYER_NAME`, `SELLER_NAME`).

### REDEFINES rules

A `REDEFINES` gives the same bytes two meanings, for example a payment or a refund. A field such as a record type usually says which. Without a rule Flow reads every meaning, and the one that does not fit a record stops the run with a message that names the field and suggests a rule.

Under **REDEFINES rules**, click **Add rule** and fill in: Read **PAYMENT-INFO** when **REC-TYPE** is **P**. Several values are separated by commas. After **Check copybook**, the boxes suggest the parts and fields of your copybook. The other part's columns are empty for that record. A record that no rule matches reads the parts that have no rule; if every part has a rule, its REDEFINES columns are empty and the run log counts such records.

Several 01 records work the same way: Read **DETAIL-REC** when **HDR-TYPE** is **D**.

### What stops with a message

- `SYNCHRONIZED`: it adds hidden slack bytes, which Flow does not lay out yet.
- `POINTER`, `INDEX`, `COMP-X` and DBCS (`PIC G`) fields.
- A `COPY` statement inside the copybook: paste the included copybook in its place.
- `OCCURS DEPENDING ON` inside another table or inside a REDEFINES.

## PGP-encrypted files

### Keep the key in Credentials

1. Open **Credentials**, click **Add**, and choose the category **PGP key**. Three fields appear:
   - **private_key**: your private key, to decrypt files sent to you and to sign files you send. Paste the armored key (`-----BEGIN PGP PRIVATE KEY BLOCK-----`) or click **Load key file**.
   - **passphrase**: the key's passphrase.
   - **public_key**: a partner's public key, to encrypt files for them and to check their signatures.
   Remove the fields you do not need. One credential per partner can hold both your key and theirs.
2. Save. The key and passphrase are stored encrypted and are never shown again, and never written to a log, a run record or a pipeline version. A pipeline keeps only the credential's id.

To export a key from GnuPG: `gpg --export-secret-keys --armor you@example.com > private.asc` (and `gpg --export --armor partner@example.com` for a public key).

### Decrypt an incoming file

Turn on **Decrypt with PGP** and choose the credential. **Check key** shows the key's names and fingerprint and says whether the passphrase unlocks it. The file is decrypted into a private temporary folder, read with the chosen format, and the decrypted copy is removed. With **Format Override** on Auto, a name like `orders.csv.pgp` is read as CSV.

Flow reads files from GnuPG, Kleopatra, OpenPGP.js, Sequoia and file-transfer products that use Bouncy Castle:

- keys made with RSA, Curve25519, Curve448, NIST P-256, P-384 and P-521, Brainpool or Elgamal, in the usual version 4 format or the newer version 6 format (RFC 9580);
- private keys protected the way GnuPG exports them, or with the newer AEAD and Argon2 protection;
- the classic encrypted format every tool writes, the OCB format GnuPG 2.3 and later write when the receiving key asks for it, and the RFC 9580 format (OCB, GCM or EAX) from OpenPGP.js and Sequoia;
- binary or armored (`.asc`), compressed or not, encrypted to a key or with a passphrase.

A signed file is read and the run log notes who signed it. To make the signature count, turn on **Check the sender's signature**.

Refused, with a message that says why:

- A file whose integrity check fails: it was changed or damaged after it was encrypted, or cut short. Nothing is read from it.
- Old files with no integrity check at all.

### Check the sender's signature

Under **Decrypt with PGP**, turn on **Check the sender's signature** and choose the credential with the sender's public key (it can be the same credential if it also holds their key). **Check key** shows which part of their key signs files.

The run then stops, and nothing is read, unless the file was signed with the sender's key and has not changed since. It stops with a message when:

- the file is not signed;
- it was signed with a different key, for example a new key the sender has not given you yet;
- the file was changed after it was signed;
- the sender's key was revoked, or had expired when the file was signed;
- the signature uses an old hash that can be forged (MD5, SHA-1 or RIPEMD-160).

A signing subkey counts only if the sender's main key has bound it and it has signed back, as GnuPG requires, so a subkey someone added to a copy of the key is not trusted. Signatures made by RSA, DSA, ECDSA, EdDSA, Ed25519 and Ed448 keys are checked, in binary or text mode.

The run log records the result, for example `signature checked: signed by 'Acme Bank <files@acme.example>' with key 6D60 A4C9 ...`. A file that is signed but not encrypted is read the same way, when this setting is on.

### Encrypt an outgoing file

In a **Destination** set to **Write the file as: Any format**, turn on **Encrypt with PGP** and choose the credential with the partner's public key. Flow writes the file in a private temporary folder, encrypts it, and saves only the encrypted file, adding `.pgp` to the name (or `.asc` with **Armored text**). Files are encrypted with AES-256, compressed, and integrity-protected in the classic format, which every current PGP tool reads, even when the partner's key asks for a newer format.

### Sign an outgoing file

Under **Encrypt with PGP**, turn on **Sign with your key** and choose the credential with your private key and its passphrase. **Check key** shows which part of your key signs and whether the passphrase unlocks it. The file is signed and then encrypted, as `gpg --sign --encrypt` does, so the partner's PGP tool can confirm that it came from you and was not changed: GnuPG prints `Good signature from ...`. The signature uses SHA-256, or SHA-384 or SHA-512 for keys that need a longer hash. Give the partner your public key once: `gpg --export --armor you@example.com`.

## Files on an FTP or SFTP server

Choose the saved FTP or SFTP connection in the **Source** step, then **Read the file as: Any format**. **File** is then the path on the server; a path without a leading `/` is under the connection's **Remote Path**. The file is downloaded in binary to a private temporary folder, decrypted if asked, and read with any format above.

## Files on a Windows file share

Mainframe extracts often land on a Windows file share, such as `\\fileserver01\mainframe\outbound`. Flow reads files from a share and writes files to it with a saved sign-in. This works the same whether Flow runs on Windows, Linux or Docker, because Flow connects to the share itself (SMB 2 and 3) rather than through the server's mapped drives.

### Save the connection

1. Open **Connections**, click **New Connection** and choose **Windows File Share (SMB)** under **Files & Storage**.
2. Fill in:
   - **Server**: the server's name or IP address, for example `fileserver01` or `fileserver01.corp.example.com`.
   - **Share**: the part after the server, `mainframe` in `\\fileserver01\mainframe`.
   - **Folder** (optional): a folder inside the share, for example `outbound\daily`. File paths in pipeline steps start here.
   - **Domain**: the account's Windows domain, for example `CORP`. Leave it blank for an account that exists only on that server. A user name typed as `CORP\svc_flow` or `svc_flow@corp.example.com` works too.
   - **User name** and **Password**: an account that can read the folder, and change it if pipelines write there. The password is stored encrypted and is never shown again.
   - **Port**: 445, unless your network team says otherwise.
   - **Require encryption**: connect only if the server encrypts the traffic (SMB 3). A share that is set to require encryption is always encrypted, whatever this box says.
3. Click **Test connection**. Flow signs in and counts what is in the folder.

### Read a file from the share

In a **Source** step, choose the file share connection. The File settings open straight away, because the File step is how Flow reads a share. **File** is the path inside the connection's folder, for example `CUSTMAST.DAT` or `2026\CUSTMAST.DAT`; forward slashes work too.

- A path that starts with `\` starts at the top of the share instead of the connection's folder.
- A full path such as `\\fileserver01\mainframe\outbound\CUSTMAST.DAT` works if it names the connection's own server and share. A path on any other server is refused, so the saved password is only ever sent to that one server.
- `*` and `?` in the file's name read every matching file into one table, in name order: `CUST*.DAT` reads `CUST01.DAT` and `CUST02.DAT`. Upper and lower case match each other, as in Windows. Folder names cannot contain `*` or `?`.

Each file is copied in binary to a private temporary folder, decrypted if **Decrypt with PGP** is on, read with any of the formats above, and the copy is removed. A file that another program is still writing is not read: the run stops and says the file is in use, and the next run reads it.

### Write a file to the share

In a **Destination** step, choose the file share connection and give the **Output File**, for example `results\orders.csv`. Missing folders are created. Flow writes the file in a private temporary folder (and encrypts it, with **Encrypt with PGP**), copies it to the share under a temporary name ending in `.partial`, and then renames it, replacing any file of the same name. A program that watches the folder never sees half a file.

### Start a pipeline when a file lands on the share

Instead of running the pipeline at a set time, let each new extract start it:

1. In the pipeline, add a text parameter such as `file_path` (**Parameters**), use the file share connection in the **Source** step, and set **File** to `${param.file_path}`. Publish the pipeline.
2. On the **Pipelines** page, click the pipeline's schedule, choose **A file arriving**, then **A Windows file share** and the connection.
3. **Folder on the share** is inside the connection's folder, for example `daily`; leave it empty for the connection's folder, or start with `\` for the top of the share. Give the file names, for example `CUST*.DAT` (upper and lower case match each other), and choose `file_path` under **Give the file's path to**.
4. **Check the folder now** lists the matching files there, then save.

Each new file starts one run, with its full path, such as `\\fileserver01\mainframe\outbound\daily\CUSTMAST.DAT`, in `file_path`. Files already in the folder when you save are left alone unless you ask for them, and a file never runs twice; tomorrow's file of the same name, with a new time, runs again. A file that the program copying it in still has open waits until it is closed, so a run never finds it in use. See [When a file arrives](pipelines.md#72-when-a-file-arrives) for the waiting time, how often the folder is checked, and what happens when a run fails.

## When something goes wrong

| Message | What to do |
|---|---|
| `record 57 (byte 3,876), field BALANCE (bytes 28 to 32): not a packed decimal` | Check the code page and **Records**. A text-mode copy breaks packed fields. If the field is in a REDEFINES, add a rule. |
| `The file ends with a partial record` | The record length is wrong, or the file has variable-length records (choose RDW). |
| `Record 3 does not start with a valid length (RDW)` | The file has no record lengths: choose **Fixed length** or **One record per line**. |
| `Copybook line 12: ... Is the period missing at the end of ...?` | Each field in a copybook ends with a period. |
| `This file was encrypted for key ID ...` | The file was encrypted to a different key. Choose the credential with the matching private key. |
| `The passphrase is wrong` | Correct the passphrase in the credential. |
| `The file is not signed, but this step requires the sender's signature` | Ask the sender to sign the file, or turn off **Check the sender's signature**. |
| `The file was signed with key ..., which is not the sender's key in the credential` | The sender may have a new key. Ask for their current public key and update the credential. |
| `The sender's signature does not match the file` | The file was changed after it was signed, or damaged on the way. Ask the sender to send it again. |
| `The file was signed with the SHA-1 hash, which is no longer safe` | Ask the sender to sign with SHA-256 or stronger (in GnuPG: `--digest-algo SHA256`). |
| `The sender's signature cannot be accepted: the key has been revoked` | The sender withdrew that key. Ask for their current public key. |
| `... is PGP-encrypted. Turn on 'Decrypt with PGP'` | The file name ends in `.pgp` or `.gpg`, or the file starts as an armored PGP message. |
| `fileserver01 refused the sign-in` | Check the domain, user name and password in the connection. The account may also be locked, disabled or have an expired password. |
| `fileserver01 has no share called '...'` | Check **Share**: the part after the server in `\\fileserver01\share`. |
| `Could not reach fileserver01 on port 445` | Check the server's name, and that the Flow server can reach it. Firewalls often block port 445, especially from cloud networks and Docker hosts. |
| `The account has no permission to use ...` | Ask for permission on both the share and the folder: read to read files, modify to write them. |
| `... is open in another program` | Another program is writing the file, or has it locked. The next run reads it. |
| `No file in ... matches '...'` | Check the name pattern and the connection's folder. |
| `... is not on \\fileserver01\share` | The path names another server or share. Save a connection for that share. |

## Limits

- Windows file shares: a file arriving on a share is noticed at the next check of the folder (every 30 seconds at most, every minute unless changed), not the moment it lands, and only files directly in the watched folder count. Flow signs in to a share only with the account saved in the connection. On a Windows server, a File path such as `\\fileserver01\share\file.dat` with no connection is still read with the account the Flow server runs under. Reading, writing and watching shares are Beta: they were tested with a simulated file server, not yet a real Windows server.
- The **Dev Sample Limit** applies to these formats too: an editor run reads only the first records.
- Large mainframe files are read in batches of 50,000 records, so memory stays flat; reading is done record by record in Python, which is slower than Flow's CSV and Parquet readers.
- PGP: a signature in a separate file (`.sig`) and the clear-signed text format (`-----BEGIN PGP SIGNED MESSAGE-----`) are not read yet. Flow cannot yet encrypt to, or sign with, a version 6 key; ask for a version 4 key. Signing is done together with encryption, not on its own.
