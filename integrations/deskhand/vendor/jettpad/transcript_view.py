"""Incremental Tk transcript; one marked range per stable message identity."""
from bisect import bisect_left
from datetime import datetime
import hashlib
import os
import uuid
from pathlib import Path

LABELS = {
    'codex_running': 'Codex is thinking…',
    'codex_failed': 'Codex could not answer · not resent',
    'codex_interrupted': 'Codex response interrupted · not automatically retried',
    'selected_running': 'Local model is thinking…',
    'selected_failed': 'Local model failed · no cloud send',
    'selected_interrupted': 'Local reply interrupted · not automatically retried',
    'image_running': 'Generating image locally…',
    'image_interrupted': 'Image generation was interrupted; send a new request to retry',
    'image_failed': 'Image generation failed; see the message below',
    'uncertain': 'Delivery unconfirmed · check Jett before resending',
    'browser_queued': 'Waiting for the Muse connection',
    'browser_sending': 'Waiting for Muse in the browser…',
    'browser_abandoned': 'Left unconfirmed after your review · not resent',
    'offline_running': 'The local stand-in is thinking…',
    'offline_failed': 'Local response failed · prompt preserved, not sent to cloud',
    'offline_interrupted': 'Local response interrupted · not automatically replayed',
}


class TranscriptView:
    def __init__(self, widget, documents=None):
        self.widget = widget
        self.documents = Path(documents) if documents is not None else None
        self.image_refs = {}
        self.rows, self.order = {}, []
        self.initialized = False

    @staticmethod
    def marks(identity):
        name = 'message_'+hashlib.sha256(identity.encode()).hexdigest()
        return name+'_start', name+'_end'

    @staticmethod
    def key(row):
        return row['ts'], row['position'], row['id']

    def _insert(self, row):
        widget = self.widget
        key = self.key(row)
        index = bisect_left(self.order, key)
        at = widget.index(self.marks(self.order[index][2])[0] if index < len(self.order) else 'end-1c')
        # A temporary LEFT mark tracks the beginning while existing RIGHT
        # starts move past the new block (including adjacent replacements).
        widget.mark_set('inserting_message', at)
        widget.mark_gravity('inserting_message', 'left')
        try:
            stamp = datetime.fromtimestamp(row['ts']).astimezone().strftime('%H:%M:%S')
        except (ValueError, OverflowError, OSError):
            stamp = '—'
        from assistant_presentation import present
        who,body=present(row)
        if row['sender']=='phil':who='Phil'
        pieces = [(who, row['sender']), ('  '+stamp+'\n', 'meta'), (body+'\n', 'body')]
        if row.get('delivery') in LABELS:
            pieces.append((LABELS[row['delivery']]+'\n', 'meta'))
        # Insert at a RIGHT mark so Tcl character accounting handles Unicode.
        widget.mark_set('insertion_end', at)
        widget.mark_gravity('insertion_end', 'right')
        for text, tag in pieces:
            widget.insert('insertion_end', text, tag)
        if self.documents is not None and row['sender']=='system' and row['id'].startswith('image:') and row['text'].startswith('Local image generated: '):
            try:
                from PIL import Image, ImageTk
                image_id=str(uuid.UUID(row['id'][6:]))
                path=self.documents/'Jett Images'/(image_id+'.png')
                if path.is_file():
                    with Image.open(path) as picture:
                        picture.thumbnail((300,300)); thumbnail=ImageTk.PhotoImage(picture.copy())
                    self.image_refs[row['id']]=thumbnail
                    image_start=widget.index('insertion_end')
                    widget.image_create('insertion_end',image=thumbnail,padx=2,pady=6)
                    tag='image_open_'+hashlib.sha256(row['id'].encode()).hexdigest()
                    widget.tag_add(tag,image_start,'insertion_end')
                    widget.tag_bind(tag,'<Button-1>',lambda event,p=path:os.startfile(str(p)))
                    widget.insert('insertion_end','\nClick the image to open it.\n','meta')
            except (OSError,ValueError,ImportError):pass
        start, end = self.marks(row['id'])
        widget.mark_set(start, 'inserting_message'); widget.mark_gravity(start, 'right')
        widget.mark_set(end, 'insertion_end'); widget.mark_gravity(end, 'left')
        widget.mark_unset('inserting_message', 'insertion_end')
        self.order.insert(index, key)
        self.rows[row['id']] = dict(row)

    def apply(self, changes):
        """Return only changed text/status receipts, excluding delivery-only edits."""
        widget = self.widget
        receipts = []
        widget.configure(state='normal')
        try:
            if not self.initialized or not self.rows:
                widget.delete('1.0', 'end')  # initial empty-state message only
            for identity, row in changes.items():
                old = self.rows.get(identity)
                if old == row:
                    continue
                if old is not None:
                    start, end = self.marks(identity)
                    widget.delete(start, end); widget.mark_unset(start, end)
                    self.order.remove(self.key(old)); del self.rows[identity]; self.image_refs.pop(identity,None)
                if row is not None:
                    self._insert(row)
                    if old is None or (old['text'],old['status']) != (row['text'],row['status']):
                        receipts.append((identity,row['status'],len(row['text'])))
            if not self.rows:
                widget.delete('1.0','end')
                widget.insert('end', "A quiet place for Jett's messages.\n", 'meta')
            self.initialized = True
        finally:
            widget.configure(state='disabled')
        return receipts
