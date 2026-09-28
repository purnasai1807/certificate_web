import { CertificateTemplate, TemplateFieldConfig } from '../types';
import { apiClient, ApiResponse, getAuthToken } from './client';

export const templatesService = {
  getTemplates(): Promise<ApiResponse<CertificateTemplate[]>> {
    return apiClient('/templates');
  },

  getTemplateById(id: string): Promise<ApiResponse<CertificateTemplate>> {
    return apiClient(`/templates/${id}`);
  },

  async uploadTemplate(file: File, customName?: string): Promise<ApiResponse<CertificateTemplate>> {
    const formData = new FormData();
    formData.append('file', file);
    if (customName) formData.append('name', customName);
    return apiClient('/templates', { method: 'POST', body: formData });
  },

  setActiveTemplate(id: string) {
    return apiClient<{ templateId: string; active: boolean }>(`/templates/${id}/activate`, { method: 'POST' });
  },

  updateTemplateFields(templateId: string, fields: TemplateFieldConfig[]) {
    return apiClient<CertificateTemplate>(`/templates/${templateId}/fields`, {
      method: 'PUT',
      body: JSON.stringify({ fields }),
    });
  },


  getTemplateVersions(id: string) {
    return apiClient<Array<{ version: number; savedAt?: string; current: boolean; fields: TemplateFieldConfig[] }>>(`/templates/${id}/versions`);
  },
  restoreTemplateVersion(id: string, version: number) {
    return apiClient<CertificateTemplate>(`/templates/${id}/versions/${version}/restore`, { method: 'POST' });
  },
  async previewTemplatePdf(id: string, participant: Record<string, string>): Promise<Blob> {
    const token = getAuthToken();
    const response = await fetch(`/api/v1/templates/${encodeURIComponent(id)}/preview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify({ participant }),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new Error(payload?.error?.message || `Preview failed (${response.status})`);
    }
    return response.blob();
  },
  deleteTemplate(id: string) {
    return apiClient<boolean>(`/templates/${id}`, { method: 'DELETE' });
  },
};
