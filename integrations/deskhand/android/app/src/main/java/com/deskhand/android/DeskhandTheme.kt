package com.deskhand.android

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Typography
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * Deskhand shared design tokens — v100.009 (frontend polish, look only).
 *
 * Single source of truth for the Android side of the shared design system
 * (see builds/100007/Deskhand-design-system.md §§1–3). The values below
 * are the exact light-mode tokens from §1. The dark values the viewer and
 * agents drawer actually use are defined below; the spec-only dark tokens
 * with no live reader were removed in 100.030 (B2-G). v100.009 stays
 * light, exactly as today (the manifest still declares
 * Theme.Material.Light.NoActionBar; do NOT flip it). [DeskhandTheme]
 * always applies the light scheme in this round.
 *
 * Rules (design doc §8):
 * - No colour, size or spacing value outside this file / §§1–3.
 * - Composables use Material3 colorScheme roles (background, surface,
 *   onSurface, onSurfaceVariant, primary) and the semantic colours
 *   below — never a hardcoded hex in a screen.
 * - Status wording is unchanged; only dot + colour change (§5).
 * - No behaviour change: no STOP / grant / auth / relay / service logic.
 */

// ---------------------------------------------------------------------------
// Light-mode tokens — Deskhand-design-system.md §1 (exact hexes)
// ---------------------------------------------------------------------------

/** Screen background. */
val DeskhandBackground: Color = Color(0xFFF7F8FA)

/** Card / dialog / list background. */
val DeskhandSurface: Color = Color(0xFFFFFFFF)

/** Primary accent: primary buttons, links, clickable version label. */
val DeskhandPrimary: Color = Color(0xFF2F6B66)

//** Accent soft surface: selected row, changelog highlight, info tint. */
val DeskhandAccentSoft: Color = Color(0xFFEAF2F1)

/** All body text, headings, list rows. */
val DeskhandTextPrimary: Color = Color(0xFF1C2530)

/** Relay / node lines, version label, timestamps, hints. */
val DeskhandTextSecondary: Color = Color(0xFF5B6B7A)

/** Connected dot, "Streaming to…" state, success text. */
val DeskhandSuccess: Color = Color(0xFF2E7D4F)

/** Warning text on white (dark amber, readable). */
val DeskhandWarningText: Color = Color(0xFF8A5A00)

/** Warning banner / row background. */
val DeskhandWarningSurface: Color = Color(0xFFFFF4D6)

/** Warning banner border. */
val DeskhandWarningBorder: Color = Color(0xFFE3C878)

/** Failed dot, error text. Red is reserved for things actually broken. */
val DeskhandError: Color = Color(0xFFB3261E)

/** Error banner background (rare; only for Failed states). */
val DeskhandErrorSurface: Color = Color(0xFFFDECEA)

/** GroupBox / card / list-divider borders. */
val DeskhandBorder: Color = Color(0xFFD9DEE4)

/** Disabled buttons / labels only. */
val DeskhandDisabledText: Color = Color(0xFF9AA8B5)

// ---------------------------------------------------------------------------
// Dark-mode tokens — Deskhand-design-system.md §1. The viewer and the
// agents drawer use the dark values below (they are live, not spec-only).
// B2-G: the dark tokens with no live reader (background, surface, text
// primary, primary, border) were removed with the dead dark colour scheme.
// ---------------------------------------------------------------------------

/** Dark text secondary (used by the viewer connection line). */
val DeskhandDarkTextSecondary: Color = Color(0xFF9AA8B5)

/** Dark success (future theme work only). */
val DeskhandDarkSuccess: Color = Color(0xFF5CB87F)

/** Dark warning text (future theme work only). */
val DeskhandDarkWarningText: Color = Color(0xFFE8B93E)

/** Dark warning surface (future theme work only). */
val DeskhandDarkWarningSurface: Color = Color(0xFF332A12)

/** Dark error (future theme work only). */
val DeskhandDarkError: Color = Color(0xFFF28B82)

/// B2-G: DeskhandSemanticColors / LightDeskhandSemanticColors /
// DarkDeskhandSemanticColors / LocalDeskhandSemanticColors removed — the
// semantic-colour CompositionLocal had no reader anywhere; status lines
// use the token vals directly.

// ---------------------------------------------------------------------------
// Material3 colour schemes — roles, not hardcoded hexes, in composables.
// ---------------------------------------------------------------------------

val DeskhandLightColorScheme = lightColorScheme(
    background = DeskhandBackground,
    surface = DeskhandSurface,
    onSurface = DeskhandTextPrimary,
    onSurfaceVariant = DeskhandTextSecondary,
    primary = DeskhandPrimary,
    onPrimary = DeskhandSurface,
    primaryContainer = DeskhandAccentSoft,
    onPrimaryContainer = DeskhandPrimary,
    surfaceVariant = DeskhandAccentSoft,
    error = DeskhandError,
    onError = DeskhandSurface,
    errorContainer = DeskhandErrorSurface,
    onErrorContainer = DeskhandError,
    outline = DeskhandBorder
)

// ---------------------------------------------------------------------------
// Typography — three sizes only (design doc §2). One family per platform:
// the Material default (Roboto / system) via MaterialTheme.typography.
// Do not set a custom font family. Dialog titles use titleLarge (22 sp).
// ---------------------------------------------------------------------------

val DeskhandTypography = Typography(
    // Title: "Deskhand" wordmark (Material3 headlineMedium default, 28 sp).
    headlineMedium = TextStyle(fontSize = 28.sp),
    // Dialog titles (22 sp) — same role as the Windows 14 pt Bold title.
    titleLarge = TextStyle(fontSize = 22.sp),
    // Body: everything the user reads or taps (14 sp).
    bodyMedium = TextStyle(fontSize = 14.sp),
    // Secondary / small: version label, changelog body, timestamps (12 sp).
    bodySmall = TextStyle(fontSize = 12.sp)
)

// 100.013: viewer typography — the live themes (root MaterialTheme in
// MainActivity and DeskhandViewerTheme) never used DeskhandTypography, so
// bumping it alone would change nothing on screen. This object is the one
// actually wired in (typography = DeskhandViewerTypography, in both
// themes): every role ~1.20x the Material3 default, whole app.
val DeskhandViewerTypography = Typography(
    headlineMedium = TextStyle(fontSize = 34.sp),
    titleLarge = TextStyle(fontSize = 26.sp),
    titleMedium = TextStyle(fontSize = 19.sp),
    bodyMedium = TextStyle(fontSize = 17.sp),
    bodySmall = TextStyle(fontSize = 14.sp),
    labelMedium = TextStyle(fontSize = 14.sp),
    labelSmall = TextStyle(fontSize = 13.sp)
)

// ---------------------------------------------------------------------------
// Spacing — one set of numbers (design doc §3). dp everywhere, never px.
// ---------------------------------------------------------------------------

object DeskhandSpacing {
    /** Outer screen padding. */
    val Outer: Dp = 16.dp

    /** Vertical gap between stacked sections. */
    val Section: Dp = 8.dp

    /** Horizontal gap between buttons / controls in a row. */
    val Control: Dp = 6.dp

    /** Inside a card / dialog. */
    val CardInner: Dp = 16.dp

    /** Dialog content padding. */
    val DialogContentPadding: Dp = 16.dp

    /** Changelog dialog max height (existing value, kept). */
    val DialogMaxHeight: Dp = 400.dp

    /** Button padding, horizontal. */
    val ButtonHorizontal: Dp = 16.dp

    /** Button padding, vertical. */
    val ButtonVertical: Dp = 8.dp

    /** Button corner radius (Material default — do not override to 0 or pill). */
    val ButtonRadius: Dp = 8.dp

    /** Minimum button / row touch-target height. */
    val ButtonMinHeight: Dp = 48.dp

    /** Card corner radius. */
    val CardRadius: Dp = 8.dp

    /** Card border width: border, not shadow (0 dp elevation). */
    val CardBorder: Dp = 1.dp

    /** Status dot diameter (§5). */
    val StatusDot: Dp = 10.dp
}

// ---------------------------------------------------------------------------
// Theme entry point — LIGHT ONLY in v100.009.
// ---------------------------------------------------------------------------

/**
 * Applies the Deskhand light theme (colour scheme + typography). Dark mode
 * is NOT enabled in v100.009: the manifest still declares the light theme.
 * Do not switch this to isSystemInDarkTheme() in a polish pass.
 */
@Composable
fun DeskhandTheme(content: @Composable () -> Unit) {
    // B2-G: the semantic-colours CompositionLocal is gone (no reader);
    // the light scheme + typography are applied directly.
    MaterialTheme(
        colorScheme = DeskhandLightColorScheme,
        typography = DeskhandTypography,
        content = content
    )
}

// ---------------------------------------------------------------------------
// Status dot — design doc §5. A 10 dp circle in the token colour, placed
// immediately at the start of the status text. Dot and text always change
// together; never a dot without its text, never text recoloured without
// its dot. Wording stays verbatim (100.002 strings, unchanged).
// ---------------------------------------------------------------------------

/**
 * 10 dp status dot. Pass the §5 token for the state:
 * [DeskhandSuccess] (connected / ready), [DeskhandWarningText]
 * (degraded / needs attention), [DeskhandError] (failed), or
 * [DeskhandTextSecondary] (starting / neutral).
 */
@Composable
fun StatusDot(color: Color, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier
            .size(DeskhandSpacing.StatusDot)
            .background(color, CircleShape)
    )
}

// ---------------------------------------------------------------------------
// Frosted glass controls — contract C2 (100.043). The single shared glass
// surface for every floating chrome element over the stream. The frosted
// effect is the translucent fill + the 1.dp border, with the stream showing
// through at 0.60f alpha; Compose has no backdrop effect, so no such
// modifier or library is used here. The result is "glass". Glass owns no
// visibility (callers keep their C1
// AnimatedVisibility wrappers), no content colour/typography, and no
// elevation (always 0.dp). 100.048 (C3) may re-tokenise the values, but it
// must keep the GlassSurface name, the modifier/shape/content signature,
// and the roles (translucent surface fill + 1.dp border + 0 elevation).
// ---------------------------------------------------------------------------

/** Glass values (contract C2): fill/border alpha and border width. */
object DeskhandGlass {
    /** Glass fill alpha over the stream. */
    val SurfaceAlpha: Float = 0.60f
    /** Glass border alpha (outline role). */
    val BorderAlpha: Float = 0.30f
    /** Glass border width. */
    val BorderWidth: Dp = 1.dp
}

/**
 * Shared glass surface (contract C2). Defaults to
 * [MaterialTheme.shapes.medium]; every call site passes the shape that
 * element already has, so no new radius is introduced.
 *
 * Focus-border decision (§5.4, option (a) preferred — one border, one
 * place): [border] is optional. When null, the default C2 border is used
 * (1.dp, outline at 0.30f). A caller may pass its own [BorderStroke] —
 * the chat composer does so to keep its focused/unfocused border signal
 * (focused -> primary, unfocused -> glass border) instead of losing it
 * under the default border.
 */
@Composable
fun GlassSurface(
    modifier: Modifier = Modifier,
    shape: Shape = MaterialTheme.shapes.medium,
    border: BorderStroke? = null,
    content: @Composable () -> Unit
) {
    Surface(
        modifier = modifier,
        shape = shape,
        color = MaterialTheme.colorScheme.surface.copy(alpha = DeskhandGlass.SurfaceAlpha),
        border = border ?: BorderStroke(
            DeskhandGlass.BorderWidth,
            MaterialTheme.colorScheme.outline.copy(alpha = DeskhandGlass.BorderAlpha)
        ),
        tonalElevation = 0.dp,
        content = content
    )
}
