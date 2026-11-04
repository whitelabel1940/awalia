//! CreateProcess assigns the job atomically, before Python can run. The job handle
//! is never inherited: even an abrupt shell exit closes it and kills its children.
use crate::options::Options;
use std::{
    ffi::OsStr,
    io,
    mem::{size_of, zeroed},
    os::windows::ffi::OsStrExt,
    ptr,
    time::Duration,
};
use windows_sys::Win32::{
    Foundation::{CloseHandle, HANDLE, WAIT_OBJECT_0},
    System::{JobObjects::*, Threading::*},
};

struct Handle(HANDLE);
impl Drop for Handle {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}

pub struct Backend {
    process: Handle,
    _job: Handle,
}

fn wide(value: &OsStr) -> Vec<u16> {
    value.encode_wide().chain(Some(0)).collect()
}

// Windows CommandLineToArgvW / Python argv quoting, including trailing backslashes.
fn quote(value: &OsStr) -> Vec<u16> {
    let mut out = vec![b'"' as u16];
    let mut slashes = 0;
    for c in value.encode_wide() {
        if c == b'\\' as u16 {
            slashes += 1;
            continue;
        }
        if c == b'"' as u16 {
            out.extend(std::iter::repeat_n(b'\\' as u16, slashes * 2 + 1));
        } else {
            out.extend(std::iter::repeat_n(b'\\' as u16, slashes));
        }
        slashes = 0;
        out.push(c);
    }
    out.extend(std::iter::repeat_n(b'\\' as u16, slashes * 2));
    out.push(b'"' as u16);
    out
}

impl Backend {
    pub fn spawn(options: &Options, token: &str) -> io::Result<Self> {
        unsafe {
            let job = CreateJobObjectW(ptr::null(), ptr::null());
            if job.is_null() {
                return Err(io::Error::last_os_error());
            }
            let job = Handle(job);
            let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = zeroed();
            limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
            if SetInformationJobObject(
                job.0,
                JobObjectExtendedLimitInformation,
                &limits as *const _ as _,
                size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
            ) == 0
            {
                return Err(io::Error::last_os_error());
            }
            let mut bytes = 0;
            InitializeProcThreadAttributeList(ptr::null_mut(), 1, 0, &mut bytes);
            let mut storage = vec![0usize; bytes.div_ceil(size_of::<usize>())];
            let attributes = storage.as_mut_ptr() as LPPROC_THREAD_ATTRIBUTE_LIST;
            if InitializeProcThreadAttributeList(attributes, 1, 0, &mut bytes) == 0 {
                return Err(io::Error::last_os_error());
            }
            struct Attributes(LPPROC_THREAD_ATTRIBUTE_LIST);
            impl Drop for Attributes {
                fn drop(&mut self) {
                    unsafe {
                        DeleteProcThreadAttributeList(self.0);
                    }
                }
            }
            let _attributes = Attributes(attributes);
            if UpdateProcThreadAttribute(
                attributes,
                0,
                PROC_THREAD_ATTRIBUTE_JOB_LIST as usize,
                &job.0 as *const _ as _,
                size_of::<HANDLE>(),
                ptr::null_mut(),
                ptr::null(),
            ) == 0
            {
                return Err(io::Error::last_os_error());
            }
            let mut startup: STARTUPINFOEXW = zeroed();
            startup.StartupInfo.cb = size_of::<STARTUPINFOEXW>() as u32;
            startup.lpAttributeList = attributes;
            let mut command = quote(options.python.as_os_str());
            for arg in options.backend_args() {
                command.push(32);
                command.extend(quote(&arg));
            }
            command.push(0);
            let mut environment: Vec<_> = std::env::vars_os()
                .filter(|(k, _)| {
                    !k.to_string_lossy()
                        .eq_ignore_ascii_case("CBS_DESKTOP_TOKEN")
                })
                .collect();
            environment.push(("CBS_DESKTOP_TOKEN".into(), token.into()));
            environment.sort_by_key(|(k, _)| k.to_string_lossy().to_uppercase());
            let mut block = Vec::new();
            for (key, value) in environment {
                block.extend(key.encode_wide());
                block.push(61);
                block.extend(value.encode_wide());
                block.push(0);
            }
            block.push(0);
            let mut process: PROCESS_INFORMATION = zeroed();
            if CreateProcessW(
                wide(options.python.as_os_str()).as_ptr(),
                command.as_mut_ptr(),
                ptr::null(),
                ptr::null(),
                0,
                CREATE_NO_WINDOW | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT,
                block.as_ptr() as _,
                wide(options.repo.as_os_str()).as_ptr(),
                &startup.StartupInfo,
                &mut process,
            ) == 0
            {
                return Err(io::Error::last_os_error());
            }
            CloseHandle(process.hThread);
            Ok(Self {
                process: Handle(process.hProcess),
                _job: job,
            })
        }
    }
    pub fn exited(&self) -> bool {
        unsafe { WaitForSingleObject(self.process.0, 0) == WAIT_OBJECT_0 }
    }
    pub fn wait(&self, timeout: Duration) -> bool {
        unsafe { WaitForSingleObject(self.process.0, timeout.as_millis() as u32) == WAIT_OBJECT_0 }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn windows_quotes_spaces_quotes_and_final_slashes() {
        assert_eq!(
            String::from_utf16(&quote(OsStr::new("C:\\a b\\"))).unwrap(),
            "\"C:\\a b\\\\\""
        );
        assert_eq!(
            String::from_utf16(&quote(OsStr::new("a\"b"))).unwrap(),
            "\"a\\\"b\""
        );
    }
}
