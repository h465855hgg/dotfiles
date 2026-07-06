#define CURSORSTYLE_BLOCK 0
#define CURSORSTYLE_BLOCK_HOLLOW 1
#define CURSORSTYLE_BAR 2
#define CURSORSTYLE_UNDERLINE 3
#define CURSORSTYLE_LOCK 4

float sdRoundBox(vec2 p, vec2 halfSize, float r) {
    vec2 q = abs(p) - halfSize + r;
    return length(max(q, 0.0)) + min(max(q.x, q.y), 0.0) - r;
}

void mainImage(out vec4 fragColor, in vec2 fragCoord) {
    vec3 texel = texture(iChannel0, fragCoord / iResolution.xy).rgb;
    fragColor = vec4(texel, 1.0);

    if (iCursorVisible.x < 0.5)
        return;

    float elapsed = iTime - iTimeCursorChange;
    float speed = 10.0;
    float t = clamp(elapsed * speed, 0.0, 1.0);
    t = 1.0 - pow(1.0 - t, 3.0);

    vec4 prev = iPreviousCursor;
    vec4 cur  = mix(prev, iCurrentCursor, t);

    int style = int(floor(iCurrentCursorStyle.x + 0.5));
    float cursorHalfW = cur.z * 0.5;
    float cursorHalfH = cur.w * 0.5;

    if (style == CURSORSTYLE_BAR) {
        cursorHalfW = max(cur.z * 0.08, 1.2);
    } else if (style == CURSORSTYLE_UNDERLINE) {
        cursorHalfH = max(cur.w * 0.08, 1.2);
    }

    vec2 curCenter  = vec2(cur.x + cur.z * 0.5, cur.y - cur.w * 0.5);
    vec2 prevCenter = vec2(prev.x + prev.z * 0.5, prev.y - prev.w * 0.5);

    float alpha = 0.0;

    const int TRAIL = 12;
    for (int i = 1; i <= TRAIL; i++) {
        float f = float(i) / float(TRAIL);
        vec2 tc = mix(curCenter, prevCenter, f);
        vec2 p = fragCoord.xy - tc;
        float d = sdRoundBox(p, vec2(cursorHalfW, cursorHalfH), 3.0);
        float s = 1.0 - smoothstep(0.0, 1.5, abs(d));
        float a = pow(1.0 - f, 2.5) * 0.55;
        alpha = max(alpha, s * a);
    }

    vec2 p = fragCoord.xy - curCenter;
    float d = sdRoundBox(p, vec2(cursorHalfW, cursorHalfH), 3.0);
    float s = 1.0 - smoothstep(0.0, 1.5, abs(d));
    alpha = max(alpha, s * 0.92);

    fragColor.rgb = mix(fragColor.rgb, vec3(1.0) - fragColor.rgb, alpha);
}
