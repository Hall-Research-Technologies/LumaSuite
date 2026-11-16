# Code Review Fixes - LumaSuite

> **Historical record.** This describes a code review made before v1.1.0 and the three fixes it led to
> (they are still in place). It is not a current status report. Of the "Future Improvements" below, the
> following is still open: DOMPurify (not used) and a Content-Security-Policy header (not sent); server-side
> validation of RTMP URLs and ticker text length is not implemented. Since then v1.1.0 added the access gate,
> output escaping of hostnames and stream names, `X-Content-Type-Options: nosniff` on the sign-in surface and
> `Referrer-Policy: no-referrer` where a link carries a token; see `PROJECT.md` section 36 and `CHANGELOG.md`.
> Line numbers and line counts quoted below are as of that review and have since moved.

## Summary of Changes

This document describes fixes for three code quality issues identified in the code review:

- **P2 #5**: O(n²) MAC Address Search Complexity
- **P2 #7**: Inconsistent Event Handler Patterns  
- **P3 #3**: innerHTML XSS Security Risk

---

## ✅ P2 #5: O(n²) MAC Address Search - FIXED

### Problem
In `lumaserver.py` line ~1611, the scan logic used nested loops to detect when a device changes IP address by comparing MAC addresses:

```python
# OLD: O(n²) complexity
for old_ip, old in list(cache_units.items()):  # Outer: O(n)
    if old_ip == ip: continue
    mac_old = (old.get("mac") or "").strip().lower()
    if mac_old and mac_old == mac_new:  # Inner: O(n) comparison
        del cache_units[old_ip]
        break
```

**Impact**: With 100 devices = 10,000 MAC comparisons. With 1000 devices = 1,000,000 comparisons.

### Solution
Build MAC-to-IP index once before the scan loop for O(1) lookups:

```python
# NEW: O(n) complexity
# Build index once (lines 1593-1599)
mac_to_ip_index = {}
with cache_lock:
    for cached_ip, cached_unit in cache_units.items():
        cached_mac = (cached_unit.get("mac") or "").strip().lower()
        if cached_mac:
            mac_to_ip_index[cached_mac] = cached_ip

# Use O(1) lookup (line 1612)
old_ip = mac_to_ip_index.get(mac_new)
if old_ip and old_ip != ip:
    del cache_units[old_ip]
    existed = True
```

**Impact**: Reduced from O(n²) to O(n). Scan performance now scales linearly with device count.

**Files Modified**: `lumaserver.py` lines 1593-1599, 1612-1615

---

## ✅ P2 #7: Event Handler Patterns - IMPROVED

### Problem
Mixed use of `.onclick =` assignments and `addEventListener()` throughout producer.js:
- Direct `.onclick` assignments can be overwritten
- No consistent pattern for cleanup or preventing duplicate handlers
- Difficult to maintain

### Solution

#### 1. Added Security Utilities (`producer.js` lines 6-32)
```javascript
// Escape HTML to prevent XSS
function escapeHtml(unsafe) {
    if (unsafe == null) return "";
    return String(unsafe)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
}

// Prevent duplicate event listeners
const eventListenerRegistry = new WeakMap();
function addEventListenerOnce(element, event, handler, options) {
    // ... tracks and prevents duplicate handlers
}
```

#### 2. Converted Critical Global Handlers to addEventListener
- **Scan Button** (line 270): `scanBtn.addEventListener('click', ...)`
- **Add Stream Button** (lines 539-548): Used `dataset.listenerAdded` flag to prevent duplicates
- **Toggle Stream Button** (lines 555-603): Wrapped in condition to prevent duplicate handlers

#### 3. Added Explanatory Comments
For dynamic handlers inside loops (e.g., delete buttons in `paintRtmpRows()`), kept `.onclick` with comments explaining why:
```javascript
// NOTE: onclick used here because paintRtmpRows() can be called multiple times.
// onclick assignment replaces the previous handler, preventing duplicates.
del.onclick = () => { ... };
```

**Best Practice Pattern**:
- **Global/persistent elements**: Use `addEventListener()` with duplicate prevention flag
- **Dynamic/temporary elements**: Use `.onclick =` (simpler, auto-replaces)
- **Document-level delegation**: Always `addEventListener()` (already implemented in `wireRtmpApply()`)

**Files Modified**: `producer.js` lines 6-32, 270, 533-548, 555-606

---

## ✅ P3 #3: innerHTML XSS Risk - FIXED

### Problem
User-controlled ticker text was inserted directly into HTML template literal:

```javascript
// OLD: XSS vulnerability (line 890)
row.innerHTML=`
    <input type="text" value="${t.text||""}" ... >
`;
```

If `t.text = '" onload="alert(1)'`, the generated HTML becomes:
```html
<input type="text" value="" onload="alert(1)" ... >
```

This allows arbitrary JavaScript execution (XSS attack).

### Solution
Replaced innerHTML with safe DOM manipulation using `createElement()` and `.value` assignments:

```javascript
// NEW: Safe DOM construction (lines 890-943)
const textInput = document.createElement("input");
textInput.type = "text";
textInput.setAttribute("data-text", "");
textInput.placeholder = "Text";
textInput.value = t.text || "";  // Safe: .value property auto-escapes
textInput.style.flex = "1";
row.appendChild(textInput);

// ... all other inputs created safely with createElement()
```

**Why This is Safe**:
- `createElement()` creates proper DOM nodes (not parsed HTML strings)
- `.value` property assignment automatically escapes special characters
- No opportunity for HTML/script injection

**Files Modified**: `producer.js` lines 890-943

---

## Testing Recommendations

### 1. P2 #5 - MAC Search Performance
```bash
# Test with large device count (simulate 500+ devices)
curl -X POST http://localhost:5000/api/scan \
  -H "Content-Type: application/json" \
  -d '{"targets": "192.168.1.1-192.168.2.244"}'
  
# Monitor scan time - should scale linearly, not quadratically
```

### 2. P2 #7 - Event Handlers
- Open Producer UI
- Click "Add Stream" multiple times
- Verify each button click only triggers once (no duplicate handlers)
- Check browser DevTools for memory leaks over time

### 3. P3 #3 - XSS Prevention
```javascript
// Try ticker text with malicious content:
state.ticker.rows[0].text = '" onload="alert(1)';
paintTickerRows();

// Expected: Text displays as literal string, no alert() executed
// Old code: Would execute alert() (XSS)
// New code: Safe - text escaped automatically
```

---

## Performance Impact

| Issue | Before | After | Improvement |
|-------|--------|-------|-------------|
| **MAC Search** | O(n²) | O(n) | 100x faster for 100 devices |
| **Event Handlers** | Mixed patterns | Consistent | Better maintainability |
| **innerHTML XSS** | Vulnerable | Protected | Security hardened |

---

## Additional Recommendations

### Future Improvements
1. **Event Delegation**: Convert more dynamic handlers to document-level delegation
2. **DOMPurify**: Consider adding DOMPurify library for complex HTML sanitization
3. **Content Security Policy**: Add CSP headers to prevent inline script execution
4. **Input Validation**: Add server-side validation for RTMP URLs and ticker text length

### Code Patterns to Follow

✅ **DO**: Use `addEventListener()` for persistent elements
✅ **DO**: Use `.value` / `.textContent` for user data
✅ **DO**: Use `createElement()` when building dynamic HTML with user input
✅ **DO**: Add comments explaining event handler patterns

❌ **DON'T**: Use `innerHTML` with template literals containing user data
❌ **DON'T**: Mix `.onclick` and `addEventListener()` without reason
❌ **DON'T**: Add event listeners inside loops without duplicate checks

---

## Files Changed Summary

- **lumaserver.py**: MAC search optimization (9 lines changed)
- **producer.js**: Security utilities + event handlers + XSS fix (87 lines changed)

Total: ~96 lines modified across 2 files
