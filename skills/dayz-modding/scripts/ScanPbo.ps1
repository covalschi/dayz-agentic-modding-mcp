param([string]$Pbo, [string]$OutDir, [switch]$All)
# Obfuscation-tolerant PBO scanner/extractor for DayZ/Arma PBOs.
# Parses the header itself (survives decoy entries), decompresses Cprs entries with the
# distance-based LZSS validated byte-identical against Bohemia's BankRev.exe, and saves
# entries that look real: marker-substring match, config.* filename, binarized-config
# signature (\0raP), or high printable ratio. -All saves every entry regardless.
# Entries larger than 8MB are listed in _listing.txt but not extracted.
$code = @'
using System;
using System.Collections.Generic;
using System.IO;
using System.Text;
using System.Text.RegularExpressions;

public static class PboScan {
    static string[] Markers = new string[] {
        "CfgPatches","CfgVehicles","CfgSoundSets","cfgSlots","CfgMods",
        "modded class","EEKilled","SpawnLoot","GetRPCManager","JsonFileLoader",
        "ActionContinuous","OnStoreSave","CF_ModStorage","RegisterNetSyncVariable",
        "CreateInInventory","ZombieBase","AnimalBase","DayZInfected"
    };

    static int ReadAsciiz(byte[] buf, int pos, out string s) {
        int end = Array.IndexOf(buf, (byte)0, pos);
        s = Encoding.UTF8.GetString(buf, pos, end - pos);
        return end + 1;
    }

    static byte[] Lzss(byte[] data, int off, int len, int expected) {
        // Distance-based LZ77 (validated byte-identical vs BankRev.exe):
        // dist = b1 | ((b2 & 0xF0) << 4), dist == 0 means 4096;
        // length = (b2 & 0x0F) + 3; positions before start read as 0x20.
        var outb = new List<byte>(expected);
        int i = off, n = off + len;
        int flags = 0;
        while (outb.Count < expected && i < n) {
            flags >>= 1;
            if ((flags & 0x100) == 0) {
                flags = data[i++] | 0xFF00;
                if (i >= n) break;
            }
            if ((flags & 1) != 0) {
                outb.Add(data[i++]);
            } else {
                if (i + 1 >= n) break;
                int b1 = data[i], b2 = data[i+1]; i += 2;
                int dist = b1 | ((b2 & 0xF0) << 4);
                if (dist == 0) dist = 4096;
                int length = (b2 & 0x0F) + 3;
                int src = outb.Count - dist;
                for (int k = 0; k < length && outb.Count < expected; k++) {
                    outb.Add((src + k < 0) ? (byte)0x20 : outb[src + k]);
                }
            }
        }
        return outb.ToArray();
    }

    static bool Contains(byte[] hay, byte[] needle) {
        int limit = hay.Length - needle.Length;
        for (int i = 0; i <= limit; i++) {
            bool m = true;
            for (int j = 0; j < needle.Length; j++)
                if (hay[i + j] != needle[j]) { m = false; break; }
            if (m) return true;
        }
        return false;
    }

    static bool LooksInteresting(byte[] content, string name, List<byte[]> markerBytes) {
        string low = name.ToLowerInvariant();
        if (low.EndsWith("config.cpp") || low.EndsWith("config.bin")) return true;
        // binarized config/anim signature: \0raP
        if (content.Length >= 4 && content[0] == 0x00 && content[1] == (byte)'r'
            && content[2] == (byte)'a' && content[3] == (byte)'P') return true;
        foreach (var mb in markerBytes) if (Contains(content, mb)) return true;
        // plain-text heuristic: mostly printable => real script/xml/layout, not a decoy
        if (content.Length >= 64) {
            int printable = 0, probe = Math.Min(content.Length, 65536);
            for (int i = 0; i < probe; i++) {
                byte b = content[i];
                if ((b >= 0x20 && b < 0x7F) || b == 0x09 || b == 0x0A || b == 0x0D) printable++;
            }
            if ((double)printable / probe > 0.83) return true;
        }
        return false;
    }

    public static void Scan(string pbo, string outDir, bool saveAll) {
        Directory.CreateDirectory(outDir);
        byte[] buf = File.ReadAllBytes(pbo);
        int pos = 0;
        var names = new List<string>();
        var packs = new List<uint>();
        var origs = new List<int>();
        var sizes = new List<int>();
        while (true) {
            string name;
            pos = ReadAsciiz(buf, pos, out name);
            uint packing = BitConverter.ToUInt32(buf, pos);
            int orig = BitConverter.ToInt32(buf, pos + 4);
            int dsize = BitConverter.ToInt32(buf, pos + 16);
            pos += 20;
            if (packing == 0x56657273) { // 'Vers' properties entry
                while (true) {
                    string k; pos = ReadAsciiz(buf, pos, out k);
                    if (k.Length == 0) break;
                    string v; pos = ReadAsciiz(buf, pos, out v);
                }
                continue;
            }
            if (name.Length == 0 && packing == 0 && dsize == 0) break; // real terminator
            names.Add(name); packs.Add(packing); origs.Add(orig); sizes.Add(dsize);
        }
        var markerBytes = new List<byte[]>();
        foreach (var m in Markers) markerBytes.Add(Encoding.ASCII.GetBytes(m));
        var listing = new StringBuilder();
        int saved = 0, skippedBig = 0;
        for (int idx = 0; idx < names.Count; idx++) {
            int dsize = sizes[idx];
            listing.AppendLine(idx + "\t" + names[idx] + "\tpack=0x" + packs[idx].ToString("x") + "\torig=" + origs[idx] + "\tdsize=" + dsize);
            if (dsize <= 0) continue;
            int dataOff = pos; pos += dsize;
            if (dsize > 8000000) { skippedBig++; continue; } // listed above, not extracted
            byte[] content;
            if (packs[idx] == 0x43707273 && origs[idx] > 0 && origs[idx] < 32000000) { // 'Cprs'
                try { content = Lzss(buf, dataOff, dsize, origs[idx]); }
                catch { content = new byte[dsize]; Array.Copy(buf, dataOff, content, 0, dsize); }
            }
            else { content = new byte[dsize]; Array.Copy(buf, dataOff, content, 0, dsize); }
            if (saveAll || LooksInteresting(content, names[idx], markerBytes)) {
                string san = Regex.Replace(names[idx], "[^A-Za-z0-9_.-]", "_");
                if (san.Length > 80) san = san.Substring(san.Length - 80);
                File.WriteAllBytes(Path.Combine(outDir, idx.ToString("D5") + "_" + san), content);
                saved++;
            }
        }
        File.WriteAllText(Path.Combine(outDir, "_listing.txt"), listing.ToString());
        Console.WriteLine(Path.GetFileName(pbo) + ": " + names.Count + " entries, saved " + saved + ", skipped >8MB: " + skippedBig);
    }
}
'@
Add-Type -TypeDefinition $code -Language CSharp
[PboScan]::Scan($Pbo, $OutDir, [bool]$All)
