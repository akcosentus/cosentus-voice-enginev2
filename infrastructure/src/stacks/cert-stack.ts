import * as cdk from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { VoiceEngineConfig } from '../config';
import { SHARED_CERT_ARN_PARAM } from '../ssm-parameters';

export interface CertStackProps extends cdk.StackProps {
  readonly config: VoiceEngineConfig;
}

/**
 * Shared wildcard ACM certificate covering both env hostnames.
 *
 * Why one shared cert (and not per-env)
 * -------------------------------------
 * One wildcard `*.cosentusaibackend.com` covers both
 * `api.cosentusaibackend.com` (prod) and `staging.cosentusaibackend.com`.
 * The single cert produces a single DNS validation CNAME, which means
 * one record to add at GoDaddy (where `cosentusaibackend.com` DNS lives —
 * not Route 53). Renewal also runs once.
 *
 * The cert lives in an env-independent stack named simply
 * `cosentus-voice-engine-cert`. CDK synthesizes this stack regardless of
 * the `-c environment=...` context, but the stack's logical name is
 * stable across contexts so CFN treats it as the same stack on every
 * deploy. The first env to deploy creates the cert; subsequent deploys
 * either no-op or do trivial updates.
 *
 * DNS validation procedure (one-time, manual at GoDaddy)
 * ------------------------------------------------------
 * After `cdk deploy cosentus-voice-engine-cert`:
 *
 *   1. AWS Console → ACM → this certificate → expand the listed domain.
 *   2. AWS shows: "_<hash>.cosentusaibackend.com CNAME _<hash>.acm-validations.aws."
 *   3. GoDaddy → DNS for cosentusaibackend.com → add a CNAME record
 *      with the host/value AWS specified.
 *   4. ACM polls every minute. Validation completes typically within
 *      5–15 minutes of the GoDaddy record propagating.
 *
 * The cert auto-renews ~60 days before expiry against the same CNAME.
 * Don't delete it from GoDaddy after validation — ACM will fail renewal
 * if the validation record disappears.
 *
 * Subject alternative names
 * -------------------------
 * `*.cosentusaibackend.com` alone does NOT cover the apex
 * `cosentusaibackend.com` (per RFC 6125 wildcards match exactly one
 * subdomain label). We don't terminate TLS on the apex — only on
 * `api.` and `staging.` — so omitting the apex SAN is fine. If the
 * apex ever gets a service, add it here and re-validate.
 */
export class CertStack extends cdk.Stack {
  public readonly certificate: acm.ICertificate;

  constructor(scope: Construct, id: string, props: CertStackProps) {
    super(scope, id, props);
    const { config } = props;

    const wildcard = `*.${config.domainApex}`;

    const certificate = new acm.Certificate(this, 'WildcardCertificate', {
      domainName: wildcard,
      validation: acm.CertificateValidation.fromDns(),
      certificateName: 'cosentus-voice-engine-wildcard',
    });
    this.certificate = certificate;

    new ssm.StringParameter(this, 'CertificateArnParam', {
      parameterName: SHARED_CERT_ARN_PARAM,
      stringValue: certificate.certificateArn,
      description:
        `Wildcard ACM cert ARN for ${wildcard}. Shared by staging + prod ` +
        'ComputeStacks. Validation records live at GoDaddy.',
    });

    new cdk.CfnOutput(this, 'CertificateArn', {
      value: certificate.certificateArn,
      exportName: 'CosentusVoiceEngineCertArn',
    });
    new cdk.CfnOutput(this, 'WildcardDomain', { value: wildcard });
    new cdk.CfnOutput(this, 'ValidationInstructions', {
      value:
        'After first deploy: AWS Console → ACM → expand cert → copy the ' +
        '_<hash>.cosentusaibackend.com CNAME and add it at GoDaddy. Validation ' +
        'usually completes within 15 minutes of DNS propagation.',
    });
  }
}
