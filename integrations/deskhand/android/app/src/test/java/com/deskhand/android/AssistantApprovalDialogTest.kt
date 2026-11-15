package com.deskhand.android

import androidx.compose.ui.test.assertCountEquals
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class AssistantApprovalDialogTest {
    @get:Rule val compose = createComposeRule()

    @Test fun showsExactTargetAndDeniesOrApproves() {
        val decisions = mutableListOf<Boolean>()
        var denies = 0
        val request = JSONObject().put("operation", "Open file or program").put("target", "C:\\Fixture\\note.txt").put("purpose", "Read the note")
        compose.setContent { DeskhandViewerTheme { AssistantApprovalDialog(request, { decisions.add(it) }, { denies++ }) } }
        compose.onNodeWithText("Open file or program\n\nTarget: C:\\Fixture\\note.txt\n\nPurpose: Read the note").assertIsDisplayed()
        compose.onAllNodesWithText("STOP").assertCountEquals(0)
        compose.onNodeWithText("Deny").performClick()
        compose.onNodeWithText("Approve").performClick()
        assertEquals(1, denies)
        assertEquals(listOf(true), decisions)
    }
}
