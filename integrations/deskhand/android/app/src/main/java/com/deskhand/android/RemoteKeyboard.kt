package com.deskhand.android

import android.content.Context
import android.text.InputType
import android.view.KeyEvent
import android.view.View
import android.view.inputmethod.BaseInputConnection
import android.view.inputmethod.EditorInfo
import android.view.inputmethod.InputConnection
import org.json.JSONObject

/** IME composition stays local until committed, so provisional text is never duplicated remotely. */
internal class RemoteKeyboard(context: Context) : View(context) {
    init { isFocusable = true; isFocusableInTouchMode = true }
    override fun onCheckIsTextEditor() = true
    private fun key(name: String) = Model.sendInput(JSONObject().put("t", "key").put("k", name))
    private fun text(value: String) {
        // The host accepts at most 256 UTF-16 units per message.
        var start = 0
        while (start < value.length) {
            var end = minOf(start + 256, value.length)
            if (end < value.length && Character.isHighSurrogate(value[end - 1])) end--
            Model.sendInput(JSONObject().put("t", "text").put("s", value.substring(start, end)))
            start = end
        }
    }
    override fun onCreateInputConnection(outAttrs: EditorInfo): InputConnection {
        outAttrs.inputType = InputType.TYPE_CLASS_TEXT
        outAttrs.imeOptions = EditorInfo.IME_FLAG_NO_EXTRACT_UI or EditorInfo.IME_ACTION_SEND
        return object : BaseInputConnection(this, true) {
            override fun commitText(value: CharSequence?, newCursorPosition: Int): Boolean {
                value?.toString()?.let { committed ->
                    val lines = committed.split("\n")
                    lines.forEachIndexed { index, line ->
                        if (index > 0) key("enter")
                        if (line.isNotEmpty()) text(line)
                    }
                }
                editable?.clear()
                return true
            }
            override fun finishComposingText(): Boolean {
                if (editable?.isNotEmpty() == true) text(editable.toString())
                editable?.clear()
                return true
            }
            override fun performEditorAction(actionCode: Int): Boolean {
                if (actionCode == EditorInfo.IME_ACTION_SEND || actionCode == EditorInfo.IME_ACTION_DONE || actionCode == EditorInfo.IME_ACTION_GO) {
                    finishComposingText()
                    key("enter")
                    return true
                }
                return super.performEditorAction(actionCode)
            }
            override fun deleteSurroundingText(beforeLength: Int, afterLength: Int): Boolean {
                if (editable?.isNotEmpty() == true) return super.deleteSurroundingText(beforeLength, afterLength)
                repeat(beforeLength.coerceAtMost(256)) { key("backspace") }
                repeat(afterLength.coerceAtMost(256)) { key("delete") }
                return true
            }
            override fun sendKeyEvent(event: KeyEvent): Boolean {
                if (event.action != KeyEvent.ACTION_DOWN) return true
                when (event.keyCode) {
                    KeyEvent.KEYCODE_DEL -> key("backspace")
                    KeyEvent.KEYCODE_FORWARD_DEL -> key("delete")
                    KeyEvent.KEYCODE_ENTER -> key("enter")
                    else -> return super.sendKeyEvent(event)
                }
                return true
            }
        }
    }
}

