/**
 * Python's number semantics where they differ from JavaScript's.
 *
 * The port must give the same answers as the Python services, and two
 * built-ins disagree on purpose: Python's `%` takes the sign of the divisor,
 * and `round()` rounds an exact tie to the even neighbour (on the exact
 * binary value of the float, then converted back like a decimal literal).
 * Coordinates from OpenCV are float32 values such as 90.0625, which are exact
 * ties at three decimals, so this matters for every snapshot.
 */

/** Python's `a % b` for numbers: the result has the sign of `b`. */
export function pyMod(a, b) {
  const r = a % b;
  return r !== 0 && (r < 0) !== (b < 0) ? r + b : r;
}

const TWO_52 = 2n ** 52n;

/** The exact value of a finite double as mantissa × 2^exponent (BigInt mantissa). */
function exact(x) {
  const view = new DataView(new ArrayBuffer(8));
  view.setFloat64(0, x);
  const hi = view.getUint32(0);
  const lo = view.getUint32(4);
  const bits = (BigInt(hi) << 32n) | BigInt(lo);
  const sign = bits >> 63n ? -1n : 1n;
  const biased = Number((bits >> 52n) & 0x7ffn);
  const fraction = bits & (TWO_52 - 1n);
  if (biased === 0) return { sign, mantissa: fraction, exponent: -1074 };
  return { sign, mantissa: fraction | TWO_52, exponent: biased - 1075 };
}

/**
 * Python's `round(x, digits)`: half-even on the exact binary value. With
 * `digits` omitted it returns an integer, like `round(x)`.
 */
export function pyRound(x, digits = 0) {
  if (!Number.isFinite(x)) {
    if (digits === 0) throw new Error("cannot round a non-finite number to an integer");
    return x;
  }
  if (x === 0) return x;
  const { sign, mantissa, exponent } = exact(x);
  const scale = 10n ** BigInt(digits);
  // value × 10^digits = mantissa × scale × 2^exponent = numerator / denominator
  let numerator = mantissa * scale;
  let denominator = 1n;
  if (exponent >= 0) numerator <<= BigInt(exponent);
  else denominator <<= BigInt(-exponent);
  let quotient = numerator / denominator;
  const twice = (numerator - quotient * denominator) * 2n;
  if (twice > denominator || (twice === denominator && quotient % 2n === 1n)) quotient += 1n;
  const text = `${sign < 0n ? "-" : ""}${quotient}e-${digits}`;
  const result = Number(text);
  return result === 0 && sign < 0n ? -0 : result;
}

// Every character for which Python's str.isspace() is true (str.strip() set).
const PY_SPACE = "[\\t\\n\\v\\f\\r\\x1c-\\x1f \\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000]";
const PY_STRIP = new RegExp(`^${PY_SPACE}+|${PY_SPACE}+$`, "gu");

/** Python's `str.strip()`: not String#trim, whose set differs (BOM, U+001C-U+001F). */
export function pyStrip(value) {
  return value.replace(PY_STRIP, "");
}

/** Python's `len(str)`: code points, not UTF-16 units. */
export function pyLength(value) {
  return [...value].length;
}

/** Python truthiness for JSON values: None, False, 0, "", [] and {} are false. */
export function pyTruthy(value) {
  if (value === null || value === undefined || value === false) return false;
  if (typeof value === "number") return value !== 0;
  if (typeof value === "string") return value.length > 0;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value).length > 0;
  return true;
}

/** `isinstance(value, dict)` for parsed JSON. */
export function isPlainObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** `isinstance(value, (int, float)) and not isinstance(value, bool)`. */
export function isNumber(value) {
  return typeof value === "number";
}

/**
 * Length of Python's `json.dumps(value, ensure_ascii=False)`: the same text
 * as JSON.stringify but with ", " and ": " separators.
 */
export function pyJsonLength(value) {
  const text = JSON.stringify(value);
  if (text === undefined) return 0;
  let extra = 0;
  let inString = false;
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    if (inString) {
      if (char === "\\") i += 1;
      else if (char === "\"") inString = false;
    } else if (char === "\"") {
      inString = true;
    } else if ((char === "," || char === ":") && text[i + 1] !== "]" && text[i + 1] !== "}") {
      extra += 1;
    }
  }
  return text.length + extra;
}
