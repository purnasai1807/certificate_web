import JSZip from 'jszip';
import { Certificate } from '../types';
import { API_BASE_URL, getAuthToken } from '../api/client';

export const sanitizeFilename = (str: string): string => str.trim().replace(/[^a-zA-Z0-9_\- ]/g, '').replace(/\s+/g, '_');

async function fetchCertificatePdf(cert: Certificate): Promise<ArrayBuffer> {
  const token = getAuthToken();
  const response = await fetch(`${API_BASE_URL}/certificates/${encodeURIComponent(cert.id)}/download`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!response.ok) {
    let message = `Certificate download failed (${response.status})`;
    try {
      const data = await response.json();
      message = data?.error?.message || message;
    } catch { /* response was not JSON */ }
    throw new Error(message);
  }
  return response.arrayBuffer();
}

export const downloadSingleCertificatePdf = async (cert: Certificate): Promise<void> => {
  const pdf = await fetchCertificatePdf(cert);
  const blob = new Blob([pdf], { type: 'application/pdf' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  const participantPart = sanitizeFilename(cert.participantName) || 'Participant';
  const idPart = sanitizeFilename(cert.participantRollNumber || cert.participantStudentId || cert.certificateId);
  link.download = `${participantPart}_${idPart || cert.certificateId}.pdf`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
};

export const downloadCertificatesZip = async (
  certificates: Certificate[],
  onProgress?: (current: number, total: number, currentName: string) => void,
  zipFileName?: string,
): Promise<{ success: boolean; count: number; error?: string }> => {
  if (!certificates?.length) return { success: false, count: 0, error: 'No certificates selected for download.' };
  try {
    const zip = new JSZip();
    const folder = zip.folder('Certificates') || zip;
    let count = 0;
    for (let i = 0; i < certificates.length; i++) {
      const cert = certificates[i];
      onProgress?.(i + 1, certificates.length, cert.participantName);
      const pdf = await fetchCertificatePdf(cert);
      const participantPart = sanitizeFilename(cert.participantName) || `Participant_${i + 1}`;
      const idPart = sanitizeFilename(cert.participantRollNumber || cert.participantStudentId || cert.certificateId);
      folder.file(`${participantPart}_${idPart || cert.certificateId}.pdf`, pdf);
      count++;
    }
    const blob = await zip.generateAsync({ type: 'blob', compression: 'DEFLATE', compressionOptions: { level: 6 } });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = zipFileName || `Certificates_Batch_${new Date().toISOString().split('T')[0]}.zip`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    return { success: true, count };
  } catch (err: any) {
    return { success: false, count: 0, error: err?.message || 'Failed to download certificate PDFs.' };
  }
};
