# Personal data and erasure

Status: built.

## The problem

Remit keeps an audit trail that cannot be altered: every command's input, and for
idempotent commands its result, is kept for good. Once a module holds people's names,
email addresses and home addresses, those copies would make an erasure request under
UK GDPR (Article 17) impossible to honour without rewriting history.

## The approach: crypto-shredding

Personal data is stored in the audit trail and the idempotency records only in
encrypted form. The key that encrypts it belongs to the person it is about. Erasing a
person deletes their keys. The rows themselves are never changed, so the trail still
cannot be altered, but what they held about that person can no longer be read.

### Declaring personal data

A field is marked personal where it is declared:

```python
class Contact(Document):
    name: Annotated[Name, Personal]
    role: Name | None = None
    email: Annotated[EmailStr | None, Personal] = None
```

The mark works on documents, commands and the models nested inside them, through
tuples and lists to any depth. It goes on the whole field (`Annotated[str | None,
Personal]`), and registration refuses two things sealing could not follow rather than
store them in the clear: a mark on part of a type (`Annotated[str, Personal] | None`),
and personal data inside a mapping or a set.

What a command returns is kept too, in its idempotency record, so registration checks
the handler's return annotation by the same rules. A result may carry personal data in
documents and other models, and in tuples or lists of them. It may not carry it in a
mapping, a set, a dataclass or a `TypedDict`, or as a bare value marked personal:
return the document the value belongs to.

Events may not carry personal fields; registration refuses one that does. Events are
broadcast to every subscriber and kept for good, so they carry ids and codes, never
personal values.

### Whose data it is

A document with personal fields is a **data subject**: its id identifies the person its
personal data is about. In `parties`, that is a contact, an address, or a party that is
a person.

When a unit of work commits a command whose input or result carries personal values,
the kernel:

1. generates a data key for that unit (AES-256-GCM, from the `cryptography` library);
2. encrypts each personal value in the audit entry and the idempotency record with it;
3. links the key to every data subject the unit wrote.

A unit that carries personal values but writes no data subject is refused, and so is
its dry run. Otherwise nothing would be linked to the data key, and nothing could erase
it. Values that hold nothing personal (null, empty, `"[erased]"`) are not encrypted.

### Erasing

A module erases a subject in two steps, in one unit of work:

1. It replaces the personal fields of the live document, for example with
   `"[erased]"` or null.
2. It calls `context.erase(document)`.

`context.erase` refuses while any personal value remains in the document, at any
depth, and the kernel checks again when the handler returns, so that a write after the
call cannot put a value back. Only the module that owns the document can erase it. When
the unit commits, every data key linked to the subject is deleted. Older
audit entries then read back with `"[erased]"` in place of the personal values. An
idempotency record whose key was deleted cannot be replayed, so a retry is refused and
asked to use a new key. The erasure's own record holds nothing personal and is not
linked to the subject, so an erasure can itself be retried safely.

A data key linked to several subjects is deleted when any one of them is erased. Over-
erasure is the safe direction: one person's erasure may make another's old audit entry
partly unreadable, but never the reverse.

## What this does not cover

- **Copies a module keeps on purpose.** An issued invoice keeps the customer's name and
  address, because tax law requires it (an Article 17(3) exception). Modules decide what
  they keep, and say so.
- **Backups.** A database restored from before an erasure still holds the deleted keys.
  Each erasure also leaves an event in the log, which carries only the subject's id, so
  that erasures can be re-applied after a restore. Keeping keys in a separate key store
  (a KMS) is on the roadmap.
- **The live documents table and PostgreSQL's internals.** The live document is
  overwritten, but old row versions may persist in the database's own files until
  vacuumed.
