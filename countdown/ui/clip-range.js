// Keep the two trim handles consistent without rounding away fractional seconds.
(function (root) {
  function adjust(current, field, value, locked) {
    let start = current.start, end = current.end;
    const length = end - start;
    value = Number(value);
    if (!Number.isFinite(value)) throw new Error('Enter a valid clip time.');
    if (field === 'start') { start = value; if (locked) end = start + length; }
    else if (field === 'end') { end = value; if (locked) start = end - length; }
    else if (field === 'duration') end = start + value;
    else throw new Error('Unknown clip field.');
    start = Math.round(start * 1000) / 1000;
    end = Math.round(end * 1000) / 1000;
    const duration = Math.round((end - start) * 1000) / 1000;
    if (start < 0) throw new Error('The start cannot be before 0:00. Unlock the length to shorten this clip.');
    if (start > 86400) throw new Error('The start time is too large.');
    if (duration < .5 || duration > 120) throw new Error('The end must be 0.5 to 120 seconds after the start.');
    return {start, end, duration};
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = {adjust};
  else root.ClipRange = {adjust};
})(typeof window !== 'undefined' ? window : this);
