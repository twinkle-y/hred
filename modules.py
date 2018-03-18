import torch.nn as nn, torch, copy, tqdm
from torch.autograd import Variable
use_cuda = torch.cuda.is_available()


def max_out(x):
    # make sure s2 is even and that the input is 2 dimension
    if len(x.size()) == 2:
        s1, s2 = x.size()
        x = x.unsqueeze(1)
        x = x.view(s1, s2 // 2, 2)
        x, _ = torch.max(x, 2)

    elif len(x.size()) == 3:
        s1, s2, s3 = x.size()
        x = x.unsqueeze(1)
        x = x.view(s1, s2, s3 // 2, 2)
        x, _ = torch.max(x, 3)

    return x


# encode each sentence utterance into a single vector
class BaseEncoder(nn.Module):
    def __init__(self, vocab_size, emb_size, hid_size, num_lyr, bidi):
        super(BaseEncoder, self).__init__()
        self.hid_size = hid_size
        self.num_lyr = num_lyr
        self.drop = nn.Dropout(0.3)
        self.direction = 2 if bidi else 1
        # by default they requires grad is true
        self.embed = nn.Embedding(vocab_size, emb_size, padding_idx=10003, sparse=False)
        self.rnn = nn.GRU(input_size=emb_size, hidden_size=hid_size,
                          num_layers=num_lyr, bidirectional=bidi, batch_first=True)

    def forward(self, x, x_lens):
        bt_siz, seq_len = x.size(0), x.size(1)
        h_0 = Variable(torch.zeros(self.direction * self.num_lyr, bt_siz, self.hid_size), requires_grad=False)
        if use_cuda:
            x = x.cuda()
            h_0 = h_0.cuda()
        x_emb = self.embed(x)
        x_emb = self.drop(x_emb)
        x_emb = torch.nn.utils.rnn.pack_padded_sequence(x_emb, x_lens, batch_first=True)
        x_o, x_hid = self.rnn(x_emb, h_0)

        # move the batch to the front of the tensor
        x_hid = x_hid.view(x.size(0), -1, self.hid_size)

        """
        base_ind = np.array([ti*seq_len for ti in range(bt_siz)])
        x_o, _ = torch.nn.utils.rnn.pad_packed_sequence(x_o, batch_first=True)
        x_o = x_o.contiguous().view(-1, self.hid_size)
        x_o = x_o[base_ind + x_lens - 1, :]
        x_o = x_o.unsqueeze(1)
        print((x_o == x_hid).all()) --> true
        """

        return x_hid


# encode the hidden states of a number of utterances
class SessionEncoder(nn.Module):
    def __init__(self, hid_size, inp_size, num_lyr, bidi):
        super(SessionEncoder, self).__init__()
        self.hid_size = hid_size
        self.num_lyr = num_lyr
        self.direction = 2 if bidi else 1
        self.rnn = nn.GRU(hidden_size=hid_size, input_size=inp_size,
                          num_layers=num_lyr, bidirectional=bidi, batch_first=True)

    def forward(self, x):
        h_0 = Variable(torch.zeros(self.direction * self.num_lyr, x.size(0), self.hid_size), requires_grad=False)
        if use_cuda:
            h_0 = h_0.cuda()
        # output, h_n for output batch is already dim 0
        _, h_n = self.rnn(x, h_0)
        # move the batch to the front of the tensor
        # return h_o if you want to decode intermediate queries as well
        h_n = h_n.view(x.size(0), -1, self.hid_size)
        return h_n


# decode the hidden state
class Decoder(nn.Module):
    def __init__(self, vocab_size, emb_size, ses_hid_size, hid_size, num_lyr=1, bidi=False, teacher=True):
        super(Decoder, self).__init__()
        self.emb_size = emb_size
        self.hid_size = hid_size
        self.num_lyr = num_lyr
        self.drop = nn.Dropout(0.3)
        self.tanh = nn.Tanh()
        self.in_embed = nn.Embedding(vocab_size, emb_size, padding_idx=10003, sparse=False)
        self.rnn = nn.GRU(hidden_size=2*hid_size, input_size=emb_size,
                          num_layers=num_lyr, bidirectional=False, batch_first=True)

        self.lin1 = nn.Linear(ses_hid_size, hid_size)
        self.lin2 = nn.Linear(2*hid_size, emb_size)
        # self.lin3 = nn.Embedding(vocab_size, emb_size, padding_idx=10003, sparse=False)
        self.out_embed = nn.Linear(emb_size, vocab_size, False)
        self.log_soft2 = nn.LogSoftmax(dim=2)
        self.direction = 2 if bidi else 1
        self.teacher_forcing = teacher
        self.diversity_rate = 0.15

    def do_decode(self, siz, seq_len, ses_encoding, target=None):
        preds = []
        inp_tok = Variable(torch.ones(siz, 1).long(), requires_grad=False)
        hid_n = ses_encoding
        if use_cuda:
            inp_tok = inp_tok.cuda()
            if target is not None:
                target = target.cuda()
        log_l = 0
        for i in range(seq_len):
            if target is not None:
                inp_tok = target.select(1, i)
                inp_tok = inp_tok.unsqueeze(1)

            inp_tok_vec = self.in_embed(inp_tok)
            inp_drop_tok_vec = self.drop(inp_tok_vec)
            hid_o, hid_n = self.rnn(inp_drop_tok_vec, torch.cat((hid_n, ses_encoding), 2))
            hid_n = hid_n[:, :, :self.hid_size]
            hid_o = self.lin2(hid_o) + inp_tok_vec
            hid_o = self.out_embed(hid_o)
            preds.append(hid_o)
            # here we do greedy decoding
            op = self.log_soft2(hid_o)
            max_val, max_ind = torch.max(op, dim=2)
            inp_tok = max_ind.clone()

            if i+1 < seq_len:
                if target is not None:
                    log_l += torch.diag(op[:, :, target.select(1, i+1).data].select(1, 0))
                else:
                    log_l += max_val

        dec_o = torch.cat(preds, 1)
        return dec_o, log_l

    def forward(self, ses_encoding, x=None, x_lens=None, beam=5):
        ses_encoding = self.tanh(self.lin1(ses_encoding))
        # indicator that we are doing inference
        if x is None:
            n_candidates, final_candids = [], []
            candidates = [([1], 0)]
            gen_len = 1
            pbar = tqdm.tqdm(total=50)
            while gen_len <= 50:
                for c in candidates:
                    seq, score = c[0], c[1]
                    _target = Variable(torch.LongTensor([seq]), requires_grad=False)
                    dec_o, log_l = self.do_decode(1, len(seq), ses_encoding, _target)
                    op = self.log_soft2(dec_o)
                    op = op[:, -1, :]
                    topval, topind = op.topk(beam, 1)
                    for i in range(beam):
                        ctok, cval = topind.data[0, i], topval.data[0, i]
                        if ctok == 2:
                            # todo should I directly return this as a result or will it be stored to be compared
                            # prune it and for comparsion in final sequences
                            final_candids.append((seq + [ctok], score))
                            # todo we don't include <s> score, so ignore </s> score as well
                        else:
                            n_candidates.append((seq + [ctok], score + cval - self.diversity_rate*(i+1)))

                # hack to exponent sequence length by alpha-0.7
                n_candidates.sort(key=lambda temp: temp[1] / (1.0*len(temp[0])**0.7), reverse=True)
                candidates = copy.copy(n_candidates[:beam])
                n_candidates[:] = []
                gen_len += 1
                pbar.update(1)
            pbar.close()
            final_candids = final_candids + candidates
            final_candids.sort(key=lambda temp: temp[1] / (1.0 * len(temp[0]) ** 0.7), reverse=True)
            return final_candids[:beam]
        else:
            if use_cuda:
                x = x.cuda()
            siz, seq_len = x.size(0), x.size(1)
            ses_encoding = ses_encoding.view(self.num_lyr*self.direction, siz, self.hid_size)
            dec_o, log_l = self.do_decode(siz, seq_len, ses_encoding, x if self.teacher_forcing else None)
            return dec_o, log_l

    def set_teacher_forcing(self, val):
        self.teacher_forcing = val
