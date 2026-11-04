//! Locally synthesized PCM; sample gain never changes the system volume.
const WAV: &[u8] = include_bytes!("../../splash/chime.wav");

fn with_volume(volume: u8) -> Vec<u8> {
    let mut wav = WAV.to_vec();
    // The bundled generator writes a canonical 44-byte mono PCM16 WAV header.
    for sample in wav[44..].chunks_exact_mut(2) {
        let value = i16::from_le_bytes([sample[0], sample[1]]) as i32;
        sample.copy_from_slice(&((value * volume.min(100) as i32 / 100) as i16).to_le_bytes());
    }
    wav
}

#[cfg(windows)]
pub fn play(volume: u8) {
    // Synchronous playback on a worker keeps the in-memory WAV alive throughout playback.
    // NOSTOP avoids overlapping/replacing another chime. NODEFAULT forbids a fallback beep.
    std::thread::spawn(move || {
        #[link(name = "winmm")]
        extern "system" {
            fn PlaySoundW(sound: *const u16, module: *mut std::ffi::c_void, flags: u32) -> i32;
        }
        let wav = with_volume(volume);
        unsafe {
            PlaySoundW(
                wav.as_ptr().cast(),
                std::ptr::null_mut(),
                0x0004 | 0x0002 | 0x0010,
            );
        }
    });
}

#[cfg(not(windows))]
pub fn play(_: u8) {}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn volume_preserves_header_and_scales_pcm_without_system_volume() {
        assert_eq!(&WAV[..4], b"RIFF");
        assert_eq!(&WAV[8..16], b"WAVEfmt ");
        assert_eq!(&WAV[36..40], b"data");
        assert_eq!(with_volume(100), WAV);
        let muted = with_volume(0);
        assert_eq!(&muted[..44], &WAV[..44]);
        assert!(muted[44..].iter().all(|b| *b == 0));
        let half = with_volume(50);
        for (source, scaled) in WAV[44..].chunks_exact(2).zip(half[44..].chunks_exact(2)) {
            assert_eq!(
                i16::from_le_bytes([scaled[0], scaled[1]]),
                i16::from_le_bytes([source[0], source[1]]) / 2
            );
        }
    }
}
