import APIClient from '../../functions/APIClient';

export type YtDlpStatusType = {
  version: string | null;
  bundled: string | null;
  channel: string;
  enabled: boolean;
  latest?: string | null;
  updated?: boolean;
  checked?: number;
};

const loadYtDlpStatus = async () => {
  return APIClient<YtDlpStatusType>('/api/appsettings/ytdlp/');
};

export default loadYtDlpStatus;
