package com.deskhand.android

import android.graphics.Bitmap
import android.graphics.SurfaceTexture
import android.opengl.EGL14
import android.opengl.EGLConfig
import android.opengl.GLES11Ext
import android.opengl.GLES20
import android.os.Handler
import android.view.Surface
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** Decode to a GL texture, then copy a bounded render target; never map vendor decoder memory. */
internal class VideoTexture(private val width: Int, private val height: Int, handler: Handler,
                            private val frame: (Bitmap) -> Unit, private val failure: (Exception) -> Unit) {
    private val display = EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY)
    private var context = EGL14.EGL_NO_CONTEXT
    private var target = EGL14.EGL_NO_SURFACE
    private var textureId = 0
    private var program = 0
    private var texture: SurfaceTexture? = null
    private var output: Surface? = null
    val surface: Surface get() = output ?: error("Video surface unavailable")
    private val pixels = ByteBuffer.allocateDirect(width * height * 4)
    private val vertices = ByteBuffer.allocateDirect(16 * 4).order(ByteOrder.nativeOrder()).asFloatBuffer().apply {
        put(floatArrayOf(-1f,-1f,0f,1f, 1f,-1f,1f,1f, -1f,1f,0f,0f, 1f,1f,1f,0f)); position(0)
    }
    private var closed = false
    init {
        try {
            check(EGL14.eglInitialize(display, null, 0, null, 0))
            val configs = arrayOfNulls<EGLConfig>(1); val count = IntArray(1)
            check(EGL14.eglChooseConfig(display, intArrayOf(EGL14.EGL_RED_SIZE,8,EGL14.EGL_GREEN_SIZE,8,
                EGL14.EGL_BLUE_SIZE,8,EGL14.EGL_ALPHA_SIZE,8,EGL14.EGL_RENDERABLE_TYPE,EGL14.EGL_OPENGL_ES2_BIT,
                EGL14.EGL_SURFACE_TYPE,EGL14.EGL_PBUFFER_BIT,EGL14.EGL_NONE),0,configs,0,1,count,0) && count[0] > 0)
            context = EGL14.eglCreateContext(display,configs[0],EGL14.EGL_NO_CONTEXT,intArrayOf(EGL14.EGL_CONTEXT_CLIENT_VERSION,2,EGL14.EGL_NONE),0)
            target = EGL14.eglCreatePbufferSurface(display,configs[0],intArrayOf(EGL14.EGL_WIDTH,width,EGL14.EGL_HEIGHT,height,EGL14.EGL_NONE),0)
            check(EGL14.eglMakeCurrent(display,target,target,context))
            val ids = IntArray(1); GLES20.glGenTextures(1,ids,0); textureId = ids[0]
            GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES,textureId)
            GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES,GLES20.GL_TEXTURE_MIN_FILTER,GLES20.GL_LINEAR)
            GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES,GLES20.GL_TEXTURE_MAG_FILTER,GLES20.GL_LINEAR)
            GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES,GLES20.GL_TEXTURE_WRAP_S,GLES20.GL_CLAMP_TO_EDGE)
            GLES20.glTexParameteri(GLES11Ext.GL_TEXTURE_EXTERNAL_OES,GLES20.GL_TEXTURE_WRAP_T,GLES20.GL_CLAMP_TO_EDGE)
            val vertex = shader(GLES20.GL_VERTEX_SHADER,"attribute vec2 p; attribute vec2 t; uniform mat4 m; varying vec2 uv; void main(){gl_Position=vec4(p,0.0,1.0);uv=(m*vec4(t,0.0,1.0)).xy;}")
            val fragment = shader(GLES20.GL_FRAGMENT_SHADER,"#extension GL_OES_EGL_image_external : require\nprecision mediump float; uniform samplerExternalOES s; varying vec2 uv; void main(){gl_FragColor=texture2D(s,uv);}")
            program = GLES20.glCreateProgram(); GLES20.glAttachShader(program,vertex); GLES20.glAttachShader(program,fragment); GLES20.glLinkProgram(program)
            GLES20.glDeleteShader(vertex); GLES20.glDeleteShader(fragment)
            val linked = IntArray(1); GLES20.glGetProgramiv(program,GLES20.GL_LINK_STATUS,linked,0); check(linked[0] != 0)
            texture = SurfaceTexture(textureId).apply {
                setDefaultBufferSize(width,height)
                setOnFrameAvailableListener({ if (!closed) try { render() } catch (error: Exception) { failure(error) } },handler)
            }
            output = Surface(texture)
        } catch (error: Exception) { close(); throw error }
    }
    private fun shader(type: Int, source: String): Int {
        val shader = GLES20.glCreateShader(type); GLES20.glShaderSource(shader,source); GLES20.glCompileShader(shader)
        val compiled = IntArray(1); GLES20.glGetShaderiv(shader,GLES20.GL_COMPILE_STATUS,compiled,0)
        check(compiled[0] != 0) { GLES20.glGetShaderInfoLog(shader) }; return shader
    }
    private fun render() {
        check(EGL14.eglMakeCurrent(display,target,target,context))
        val source = texture ?: return
        source.updateTexImage()
        val matrix = FloatArray(16); source.getTransformMatrix(matrix)
        GLES20.glViewport(0,0,width,height); GLES20.glUseProgram(program)
        val position = GLES20.glGetAttribLocation(program,"p"); val coord = GLES20.glGetAttribLocation(program,"t")
        vertices.position(0); GLES20.glVertexAttribPointer(position,2,GLES20.GL_FLOAT,false,16,vertices); GLES20.glEnableVertexAttribArray(position)
        vertices.position(2); GLES20.glVertexAttribPointer(coord,2,GLES20.GL_FLOAT,false,16,vertices); GLES20.glEnableVertexAttribArray(coord)
        GLES20.glUniformMatrix4fv(GLES20.glGetUniformLocation(program,"m"),1,false,matrix,0)
        GLES20.glActiveTexture(GLES20.GL_TEXTURE0); GLES20.glBindTexture(GLES11Ext.GL_TEXTURE_EXTERNAL_OES,textureId)
        GLES20.glUniform1i(GLES20.glGetUniformLocation(program,"s"),0)
        GLES20.glDrawArrays(GLES20.GL_TRIANGLE_STRIP,0,4)
        pixels.clear(); GLES20.glReadPixels(0,0,width,height,GLES20.GL_RGBA,GLES20.GL_UNSIGNED_BYTE,pixels)
        check(GLES20.glGetError() == GLES20.GL_NO_ERROR)
        pixels.position(0)
        val bitmap = Bitmap.createBitmap(width,height,Bitmap.Config.ARGB_8888)
        bitmap.copyPixelsFromBuffer(pixels)
        frame(bitmap)
    }
    fun close() {
        if (closed) return; closed = true
        output?.release(); output = null
        texture?.release(); texture = null
        if (context != EGL14.EGL_NO_CONTEXT) {
            EGL14.eglMakeCurrent(display,target,target,context)
            if (program != 0) GLES20.glDeleteProgram(program)
            if (textureId != 0) GLES20.glDeleteTextures(1,intArrayOf(textureId),0)
        }
        EGL14.eglMakeCurrent(display,EGL14.EGL_NO_SURFACE,EGL14.EGL_NO_SURFACE,EGL14.EGL_NO_CONTEXT)
        if (target != EGL14.EGL_NO_SURFACE) EGL14.eglDestroySurface(display,target)
        if (context != EGL14.EGL_NO_CONTEXT) EGL14.eglDestroyContext(display,context)
        EGL14.eglReleaseThread(); EGL14.eglTerminate(display)
    }
}
